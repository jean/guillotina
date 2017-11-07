/*jshint unused:false */

(function (exports) {

    'use strict';

    var STORAGE_KEY = 'todos-vuejs';

    exports.todoStorage = {
        fetch: function () {
            return JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
        },
        save: function (todos) {
            localStorage.setItem(STORAGE_KEY, JSON.stringify(todos));

            // START
            todos.forEach(function (todo) {
                this.$http.post('db/todo', {
                    '@type': 'Todo',
                    text: todo.title,
                    completed: todo.completed
                }, function (data, status, request) {
                    //this.people.push(data);
                    //
                   });
                })();
            // END
        }
    };

})(window);
