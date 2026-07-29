class FailureState:
    latency_enabled: bool = False
    latency_ms: int = 500
    errors_enabled: bool = False

state = FailureState()