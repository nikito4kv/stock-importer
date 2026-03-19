# UI Layer

- `controller.py` is the boundary between desktop widgets and application use cases
- `qt_app.py` and `tk_app.py` render the same view models and should stay thin
- UI code should not reach into repositories or pipeline internals directly
