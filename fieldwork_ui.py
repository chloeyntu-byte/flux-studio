"""Product adapters for the unmodified RF-FIELDWORK theme."""
import fieldwork_tk as fw

def bind_status(variable, chip, busy=lambda: False):
    def refresh(*_):
        message = variable.get()
        lower = message.lower()
        state = 'idle'
        if any(word in lower for word in ('failed', 'could not', 'exception')):
            state = 'error'
        elif busy():
            state = 'running'
        elif any(word in lower for word in ('warning', 'unavailable', 'missing', 'cancel', 'adjust', 'targets not met', 'incomplete')):
            state = 'warn'
        elif any(word in lower for word in ('saved', 'completed', 'validated', 'loaded', 'generated')):
            state = 'done'
        chip.set(state, message)
    variable.trace_add('write', refresh)
    refresh()
    return refresh
