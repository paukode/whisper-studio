# Whisper Studio Plugins

Drop `.py` files here to add custom tools and executors.

## Plugin interface

```python
__version__ = '1.0.0'
__description__ = 'My custom plugin'

def register(app, executor_registry):
    # Register a new executor tool
    def my_tool(tool_input, transcript, attachments):
        return 'Hello from plugin!'
    executor_registry['my_tool'] = my_tool
```
