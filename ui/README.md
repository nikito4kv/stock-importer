# UI Layer

- `controller.py` is the boundary between desktop widgets and application use cases
- `qt_app.py` renders the desktop view models and should stay thin
- UI code should not reach into repositories or pipeline internals directly
