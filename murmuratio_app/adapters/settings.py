"""Model settings observed in provider records; absent settings stay unknown."""


def recorded_string(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def recorded_effort(*sources, keys=('effort',)):
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in keys:
            if key in source:
                return recorded_string(source[key])
    return None


def consume_codex_settings(data, record):
    if record.get('type') not in ('session_meta', 'turn_context'):
        return
    payload = record.get('payload')
    if not isinstance(payload, dict):
        return
    mode = payload.get('collaboration_mode')
    settings = mode.get('settings') if isinstance(mode, dict) else None
    settings = settings if isinstance(settings, dict) else {}
    keys = ('model_reasoning_effort', 'effort', 'reasoning_effort')
    effort = recorded_effort(payload, settings, keys=keys)
    if record['type'] == 'session_meta':
        data['_session_effort'] = effort
    elif not any(key in source for source in (payload, settings) for key in keys):
        # A recorded launch setting is usable; a previous turn's setting is not a default.
        effort = data.get('_session_effort')
    data['effort'] = effort
    if 'model' in payload:
        data['model'] = recorded_string(payload['model'])


def consume_claude_settings(data, record):
    kind = record.get('type')
    if kind not in ('assistant', 'api-request', 'api-request-blob'):
        return
    message = record.get('message')
    params = record.get('params')
    output = (message.get('output_config') if isinstance(message, dict) else None)
    request_output = params.get('output_config') if isinstance(params, dict) else None
    for source, keys in ((record, ('perTurnEffort', 'effort')),
                         (output, ('effort',)), (request_output, ('effort',))):
        if isinstance(source, dict) and any(key in source for key in keys):
            data['effort'] = recorded_effort(source, keys=keys)
            break
