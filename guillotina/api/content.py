from aiohttp.web_exceptions import HTTPMethodNotAllowed
from aiohttp.web_exceptions import HTTPNotFound
from aiohttp.web_exceptions import HTTPUnauthorized
from dateutil.tz import tzlocal
from guillotina import configure
from guillotina import security
from guillotina._settings import app_settings
from guillotina.annotations import AnnotationData
from guillotina.api.service import Service
from guillotina.auth.role import local_roles
from guillotina.browser import ErrorResponse
from guillotina.browser import Response
from guillotina.component import get_multi_adapter
from guillotina.component import get_utility
from guillotina.component import query_multi_adapter
from guillotina.content import create_content_in_container
from guillotina.content import get_all_behavior_interfaces
from guillotina.content import get_all_behaviors
from guillotina.event import notify
from guillotina.events import BeforeObjectMovedEvent
from guillotina.events import BeforeObjectRemovedEvent
from guillotina.events import ObjectAddedEvent
from guillotina.events import ObjectDuplicatedEvent
from guillotina.events import ObjectModifiedEvent
from guillotina.events import ObjectMovedEvent
from guillotina.events import ObjectPermissionsModifiedEvent
from guillotina.events import ObjectPermissionsViewEvent
from guillotina.events import ObjectRemovedEvent
from guillotina.events import ObjectVisitedEvent
from guillotina.exceptions import ConflictIdOnContainer
from guillotina.exceptions import NotAllowedContentType
from guillotina.exceptions import PreconditionFailed
from guillotina.i18n import default_message_factory as _
from guillotina.interfaces import IAbsoluteURL
from guillotina.interfaces import IAnnotations
from guillotina.interfaces import IFolder
from guillotina.interfaces import IGetOwner
from guillotina.interfaces import IInteraction
from guillotina.interfaces import IPrincipalPermissionManager
from guillotina.interfaces import IPrincipalPermissionMap
from guillotina.interfaces import IPrincipalRoleManager
from guillotina.interfaces import IPrincipalRoleMap
from guillotina.interfaces import IResource
from guillotina.interfaces import IResourceDeserializeFromJson
from guillotina.interfaces import IResourceSerializeToJson
from guillotina.interfaces import IRolePermissionManager
from guillotina.interfaces import IRolePermissionMap
from guillotina.json.exceptions import DeserializationError
from guillotina.json.utils import convert_interfaces_to_schema
from guillotina.profile import profilable
from guillotina.transactions import get_transaction
from guillotina.utils import get_authenticated_user_id
from guillotina.utils import iter_parents
from guillotina.utils import navigate_to
from guillotina.utils import valid_id


_zone = tzlocal()


def get_content_json_schema_responses(content):
    return {
        "200": {
            "description": "Resource data",
            "schema": {
                "allOf": [
                    {"$ref": "#/definitions/ResourceFolder"},
                    {"properties": convert_interfaces_to_schema(
                        get_all_behavior_interfaces(content))}
                ]
            }
        }
    }


def patch_content_json_schema_parameters(content):
    return [{
        "name": "body",
        "in": "body",
        "schema": {
            "allOf": [
                {"$ref": "#/definitions/WritableResource"},
                {"properties": convert_interfaces_to_schema(
                    get_all_behavior_interfaces(content))}
            ]
        }
    }]


@configure.service(
    context=IResource, method='HEAD', permission='guillotina.ViewContent')
async def default_head(context, request):
    return {}


@configure.service(
    context=IResource, method='GET', permission='guillotina.ViewContent',
    summary="Retrieves serialization of resource",
    responses=get_content_json_schema_responses,
    parameters=[{
        "name": "include",
        "in": "query",
        "type": "string"
    }, {
        "name": "omit",
        "in": "query",
        "type": "string"
    }])
class DefaultGET(Service):
    @profilable
    async def __call__(self):
        serializer = get_multi_adapter(
            (self.context, self.request),
            IResourceSerializeToJson)
        include = omit = []
        if self.request.GET.get('include'):
            include = self.request.GET.get('include').split(',')
        if self.request.GET.get('omit'):
            omit = self.request.GET.get('omit').split(',')
        try:
            result = await serializer(include=include, omit=omit)
        except TypeError:
            result = await serializer()
        await notify(ObjectVisitedEvent(self.context))
        return result


@configure.service(
    context=IResource, method='POST', permission='guillotina.AddContent',
    summary='Add new resouce inside this container resource',
    parameters=[{
        "name": "body",
        "in": "body",
        "schema": {
            "$ref": "#/definitions/AddableResource"
        }
    }],
    responses={
        "200": {
            "description": "Resource data",
            "schema": {
                "$ref": "#/definitions/ResourceFolder"
            }
        }
    })
class DefaultPOST(Service):

    @profilable
    async def __call__(self):
        """To create a content."""
        data = await self.get_data()
        type_ = data.get('@type', None)
        id_ = data.get('id', None)
        behaviors = data.get('@behaviors', None)

        if '__acl__' in data:
            # we don't allow to change the permisions on this patch
            del data['__acl__']

        if not type_:
            return ErrorResponse(
                'RequiredParam',
                _("Property '@type' is required"))

        # Generate a temporary id if the id is not given
        if not id_:
            new_id = None
        else:
            if not valid_id(id_):
                return ErrorResponse('PreconditionFailed', str('Invalid id'),
                                     status=412)
            new_id = id_

        user = get_authenticated_user_id(self.request)

        # Create object
        try:
            obj = await create_content_in_container(
                self.context, type_, new_id, id=new_id, creators=(user,),
                contributors=(user,))
        except (PreconditionFailed, NotAllowedContentType) as e:
            return ErrorResponse(
                'PreconditionFailed',
                str(e),
                status=412)
        except ConflictIdOnContainer as e:
            return ErrorResponse(
                'ConflictId',
                str(e),
                status=409)
        except ValueError as e:
            return ErrorResponse(
                'CreatingObject',
                str(e),
                status=400)

        for behavior in behaviors or ():
            obj.add_behavior(behavior)

        # Update fields
        deserializer = query_multi_adapter((obj, self.request),
                                           IResourceDeserializeFromJson)
        if deserializer is None:
            return ErrorResponse(
                'DeserializationError',
                'Cannot deserialize type {}'.format(obj.type_name),
                status=501)

        try:
            await deserializer(data, validate_all=True)
        except DeserializationError as e:
            return ErrorResponse(
                'DeserializationError',
                str(e),
                exc=e,
                status=400)

        # Local Roles assign owner as the creator user
        get_owner = get_utility(IGetOwner)
        roleperm = IPrincipalRoleManager(obj)
        roleperm.assign_role_to_principal(
            'guillotina.Owner', await get_owner(obj, user))

        data['id'] = obj.id
        await notify(ObjectAddedEvent(obj, self.context, obj.id, payload=data))

        absolute_url = query_multi_adapter((obj, self.request), IAbsoluteURL)

        headers = {
            'Access-Control-Expose-Headers': 'Location',
            'Location': absolute_url()
        }

        serializer = query_multi_adapter(
            (obj, self.request),
            IResourceSerializeToJson
        )
        response = await serializer()
        return Response(response=response, headers=headers, status=201)


@configure.service(
    context=IResource, method='PATCH', permission='guillotina.ModifyContent',
    summary='Modify the content of this resource',
    parameters=patch_content_json_schema_parameters,
    responses={
        "200": {
            "description": "Resource data",
            "schema": {
                "$ref": "#/definitions/Resource"
            }
        }
    })
class DefaultPATCH(Service):
    async def __call__(self):
        data = await self.get_data()

        if 'id' in data and data['id'] != self.context.id:
            return ErrorResponse(
                'DeserializationError',
                'Not allowed to change id of content.',
                status=412)

        behaviors = data.get('@behaviors', None)
        for behavior in behaviors or ():
            self.context.add_behavior(behavior)

        deserializer = query_multi_adapter((self.context, self.request),
                                           IResourceDeserializeFromJson)
        if deserializer is None:
            return ErrorResponse(
                'DeserializationError',
                'Cannot deserialize type {}'.format(self.context.type_name),
                status=412)

        try:
            await deserializer(data)
        except DeserializationError as e:
            return ErrorResponse(
                'DeserializationError',
                str(e),
                status=422)

        await notify(ObjectModifiedEvent(self.context, payload=data))

        return Response(response={}, status=204)


@configure.service(
    context=IResource, method='GET',
    permission='guillotina.SeePermissions', name='@sharing',
    summary='Get sharing settings for this resource',
    responses={
        "200": {
            "description": "All the sharing defined on this resource",
            "schema": {
                "$ref": "#/definitions/ResourceACL"
            }
        }
    })
async def sharing_get(context, request):
    roleperm = IRolePermissionMap(context)
    prinperm = IPrincipalPermissionMap(context)
    prinrole = IPrincipalRoleMap(context)
    result = {
        'local': {},
        'inherit': []
    }
    result['local']['roleperm'] = roleperm._bycol
    result['local']['prinperm'] = prinperm._bycol
    result['local']['prinrole'] = prinrole._bycol
    for obj in iter_parents(context):
        roleperm = IRolePermissionMap(obj, None)
        if roleperm is not None:
            prinperm = IPrincipalPermissionMap(obj)
            prinrole = IPrincipalRoleMap(obj)
            result['inherit'].append({
                '@id': IAbsoluteURL(obj, request)(),
                'roleperm': roleperm._bycol,
                'prinperm': prinperm._bycol,
                'prinrole': prinrole._bycol,
            })
    await notify(ObjectPermissionsViewEvent(context))
    return result


@configure.service(
    context=IResource, method='GET',
    permission='guillotina.SeePermissions', name='@all_permissions',
    summary='See all permission settings for this resource',
    responses={
        "200": {
            "description": "All the permissions defined on this resource",
            "schema": {
                "$ref": "#/definitions/AllPermissions"
            }
        }
    })
async def all_permissions(context, request):
    result = security.utils.settings_for_object(context)
    await notify(ObjectPermissionsViewEvent(context))
    return result


PermissionMap = {
    'prinrole': {
        'Allow': 'assign_role_to_principal',
        'Deny': 'remove_role_from_principal',
        'AllowSingle': 'assign_role_to_principal_no_inherit',
        'Unset': 'unset_role_for_principal'
    },
    'roleperm': {
        'Allow': 'grant_permission_to_role',
        'Deny': 'deny_permission_to_role',
        'AllowSingle': 'grant_permission_to_role_no_inherit',
        'Unset': 'unset_permission_from_role'
    },
    'prinperm': {
        'Allow': 'grant_permission_to_principal',
        'Deny': 'deny_permission_to_principal',
        'AllowSingle': 'grant_permission_to_principal_no_inherit',
        'Unset': 'unset_permission_for_principal'
    }
}


@configure.service(
    context=IResource, method='POST',
    permission='guillotina.ChangePermissions', name='@sharing',
    summary='Change permissions for a resource',
    parameters=[{
        "name": "body",
        "in": "body",
        "type": "object",
        "schema": {
            "$ref": "#/definitions/Permissions"
        }
    }],
    responses={
        "200": {
            "description": "Successfully changed permission"
        }
    })
class SharingPOST(Service):
    async def __call__(self, changed=False):
        """Change permissions"""
        context = self.context
        request = self.request
        lroles = local_roles()
        data = await request.json()
        if 'prinrole' not in data and \
                'roleperm' not in data and \
                'prinperm' not in data:
            raise AttributeError('prinrole or roleperm or prinperm missing')

        for prinrole in data.get('prinrole') or []:
            setting = prinrole.get('setting')
            if setting not in PermissionMap['prinrole']:
                raise AttributeError('Invalid Type {}'.format(setting))
            manager = IPrincipalRoleManager(context)
            operation = PermissionMap['prinrole'][setting]
            func = getattr(manager, operation)
            if prinrole['role'] in lroles:
                changed = True
                func(prinrole['role'], prinrole['principal'])
            else:
                raise KeyError('No valid local role')

        for prinperm in data.get('prinperm') or []:
            setting = prinperm['setting']
            if setting not in PermissionMap['prinperm']:
                raise AttributeError('Invalid Type')
            manager = IPrincipalPermissionManager(context)
            operation = PermissionMap['prinperm'][setting]
            func = getattr(manager, operation)
            changed = True
            func(prinperm['permission'], prinperm['principal'])

        for roleperm in data.get('roleperm') or []:
            setting = roleperm['setting']
            if setting not in PermissionMap['roleperm']:
                raise AttributeError('Invalid Type')
            manager = IRolePermissionManager(context)
            operation = PermissionMap['roleperm'][setting]
            func = getattr(manager, operation)
            changed = True
            func(roleperm['permission'], roleperm['role'])

        if changed:
            context._p_register()  # make sure data is saved
            await notify(ObjectPermissionsModifiedEvent(context, data))


@configure.service(
    context=IResource, method='PUT',
    permission='guillotina.ChangePermissions', name='@sharing',
    summary='Replace permissions for a resource',
    parameters=[{
        "name": "body",
        "in": "body",
        "type": "object",
        "schema": {
            "$ref": "#/definitions/Permissions"
        }
    }],
    responses={
        "200": {
            "description": "Successfully replaced permissions"
        }
    })
class SharingPUT(SharingPOST):
    async def __call__(self):
        self.context.__acl__ = None
        return await super().__call__(True)


@configure.service(
    context=IResource, method='GET',
    permission='guillotina.AccessContent', name='@canido',
    parameters=[{
        "name": "permission",
        "in": "query",
        "required": True,
        "type": "string"
    }],
    responses={
        "200": {
            "description": "Successfully changed permission"
        }
    })
async def can_i_do(context, request):
    if 'permission' not in request.query:
        raise TypeError('No permission param')
    permission = request.query['permission']
    return IInteraction(request).check_permission(permission, context)


@configure.service(
    context=IResource, method='DELETE', permission='guillotina.DeleteContent',
    summary='Delete resource',
    responses={
        "200": {
            "description": "Successfully deleted resource"
        }
    })
class DefaultDELETE(Service):

    async def __call__(self):
        content_id = self.context.id
        parent = self.context.__parent__
        await notify(BeforeObjectRemovedEvent(self.context, parent, content_id))
        self.context._p_jar.delete(self.context)
        await notify(ObjectRemovedEvent(self.context, parent, content_id))


@configure.service(
    context=IResource, method='OPTIONS', permission='guillotina.AccessPreflight',
    summary='Get CORS information for resource')
class DefaultOPTIONS(Service):
    """Preflight view for Cors support on DX content."""

    def getRequestMethod(self):  # noqa
        """Get the requested method."""
        return self.request.headers.get(
            'Access-Control-Request-Method', None)

    async def preflight(self):
        """We need to check if there is cors enabled and is valid."""
        headers = {}

        renderer = app_settings['cors_renderer'](self.request)
        settings = await renderer.get_settings()

        if not settings:
            return {}

        origin = self.request.headers.get('Origin', None)
        if not origin:
            raise HTTPNotFound(text='Origin this header is mandatory')

        requested_method = self.getRequestMethod()
        if not requested_method:
            raise HTTPNotFound(
                text='Access-Control-Request-Method this header is mandatory')

        requested_headers = (
            self.request.headers.get('Access-Control-Request-Headers', ()))

        if requested_headers:
            requested_headers = map(str.strip, requested_headers.split(', '))

        requested_method = requested_method.upper()
        allowed_methods = settings['allow_methods']
        if requested_method not in allowed_methods:
            raise HTTPMethodNotAllowed(
                requested_method, allowed_methods,
                text='Access-Control-Request-Method Method not allowed')

        supported_headers = settings['allow_headers']
        if '*' not in supported_headers and requested_headers:
            supported_headers = [s.lower() for s in supported_headers]
            for h in requested_headers:
                if not h.lower() in supported_headers:
                    raise HTTPUnauthorized(
                        text='Access-Control-Request-Headers Header %s not allowed' % h)

        supported_headers = [] if supported_headers is None else supported_headers
        requested_headers = [] if requested_headers is None else requested_headers

        supported_headers = set(supported_headers) | set(requested_headers)

        headers['Access-Control-Allow-Headers'] = ','.join(supported_headers)
        headers['Access-Control-Allow-Methods'] = ','.join(settings['allow_methods'])
        headers['Access-Control-Max-Age'] = str(settings['max_age'])
        return headers

    async def render(self):
        """Need to be overwritten in case you implement OPTIONS."""
        return {}

    async def __call__(self):
        """Apply CORS on the OPTIONS view."""
        headers = await self.preflight()
        resp = await self.render()
        if isinstance(resp, Response):
            headers.update(resp.headers)
            resp.headers = headers
            return resp
        return Response(response=resp, headers=headers, status=200)


@configure.service(
    context=IResource, method='POST', name="@move",
    permission='guillotina.MoveContent',
    summary='Move resource',
    parameters=[{
        "name": "body",
        "in": "body",
        "type": "object",
        "schema": {
            "properties": {
                "destination": {
                    "type": "string",
                    "description": "Absolute path to destination object from container",
                    "required": False
                },
                "new_id": {
                    "type": "string",
                    "description": "Optional new id to assign object",
                    "required": False
                }
            }
        }
    }],
    responses={
        "200": {
            "description": "Successfully moved resource"
        }
    })
async def move(context, request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    destination = data.get('destination')
    if destination is None:
        destination_ob = context.__parent__
    try:
        destination_ob = await navigate_to(request.container, destination)
    except KeyError:
        destination_ob = None

    if destination_ob is None:
        return ErrorResponse(
            'Configuration',
            'Could not find destination object',
            status=400)
    old_id = context.id
    if 'new_id' in data:
        new_id = data['new_id']
        context.id = context.__name__ = new_id
    else:
        new_id = context.id

    security = IInteraction(request)
    if not security.check_permission('guillotina.AddContent', destination_ob):
        return ErrorResponse(
            'Configuration',
            'You do not have permission to add content to the destination object',
            status=400)

    if await destination_ob.async_contains(new_id):
        return ErrorResponse(
            'Configuration',
            f'Destination already has object with the id {new_id}',
            status=400)

    original_parent = context.__parent__

    txn = get_transaction(request)
    cache_keys = txn._cache.get_cache_keys(context, 'deleted')

    data['id'] = new_id
    await notify(
        BeforeObjectMovedEvent(context, original_parent, old_id, destination_ob,
                               new_id, payload=data))

    context.__parent__ = destination_ob
    context._p_register()

    await notify(
        ObjectMovedEvent(context, original_parent, old_id, destination_ob,
                         new_id, payload=data))

    cache_keys += txn._cache.get_cache_keys(context, 'added')
    await txn._cache.delete_all(cache_keys)

    absolute_url = query_multi_adapter((context, request), IAbsoluteURL)
    return {
        '@url': absolute_url()
    }


@configure.service(
    context=IResource, method='POST', name="@duplicate",
    permission='guillotina.DuplicateContent',
    summary='Duplicate resource',
    parameters=[{
        "name": "body",
        "in": "body",
        "type": "object",
        "schema": {
            "properties": {
                "destination": {
                    "type": "string",
                    "description": "Absolute path to destination object from container",
                    "required": False
                },
                "new_id": {
                    "type": "string",
                    "description": "Optional new id to assign object",
                    "required": False
                }
            }
        }
    }],
    responses={
        "200": {
            "description": "Successfully duplicated object"
        }
    })
async def duplicate(context, request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    destination = data.get('destination')
    if destination is not None:
        destination_ob = await navigate_to(request.container, destination)
        if destination_ob is None:
            return ErrorResponse(
                'Configuration',
                'Could not find destination object',
                status=400)
    else:
        destination_ob = context.__parent__

    security = IInteraction(request)
    if not security.check_permission('guillotina.AddContent', destination_ob):
        return ErrorResponse(
            'Configuration',
            'You do not have permission to add content to the destination object',
            status=400)

    if 'new_id' in data:
        new_id = data['new_id']
        if await destination_ob.async_contains(new_id):
            return ErrorResponse(
                'Configuration',
                f'Destination already has object with the id {new_id}',
                status=400)
    else:
        count = 1
        new_id = f'{context.id}-duplicate-{count}'
        while await destination_ob.async_contains(new_id):
            count += 1
            new_id = f'{context.id}-duplicate-{count}'

    new_obj = await create_content_in_container(
        destination_ob, context.type_name, new_id, id=new_id,
        creators=context.creators, contributors=context.contributors)

    for key in context.__dict__.keys():
        if key.startswith('__') or key.startswith('_BaseObject'):
            continue
        if key in ('id',):
            continue
        new_obj.__dict__[key] = context.__dict__[key]
    new_obj.__acl__ = context.__acl__
    new_obj.__behaviors__ = context.__behaviors__

    # need to copy annotation data as well...
    # load all annotations for context
    [b for b in await get_all_behaviors(context, load=True)]
    annotations_container = IAnnotations(new_obj)
    for anno_id, anno_data in context.__annotations__.items():
        new_anno_data = AnnotationData()
        for key, value in anno_data.items():
            new_anno_data[key] = value
        await annotations_container.async_set(anno_id, new_anno_data)

    data['id'] = new_id
    await notify(
        ObjectDuplicatedEvent(new_obj, context, destination_ob, new_id, payload=data))

    get = DefaultGET(new_obj, request)
    return await get()


@configure.service(
    context=IFolder, method='GET', name="@ids",
    permission='guillotina.ViewContent',
    summary='Return a list of ids in the resource',
    responses={
        "200": {
            "description": "Successfully returned list of ids"
        }
    })
async def ids(context, request):
    return await context.async_keys()
