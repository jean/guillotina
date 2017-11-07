from guillotina import configure
from guillotina import schema
from guillotina import Interface
from guillotina import interfaces
from guillotina import content

class IToDo(interfaces.IItem):
    text = schema.Text()
    completed = schema.Bool()

@configure.contenttype(
        type_name="ToDo",
        schema=IToDo)
class ToDo(content.Item):
    """
    Our ToDo type
    """


