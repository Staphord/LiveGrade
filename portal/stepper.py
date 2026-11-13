"""Step data for the shared setup stepper (templates/partials/_stepper.html).

Every setup flow describes its steps as plain dicts and renders them through
the one partial, so the stepper looks and behaves the same in every service.
"""

LOCKED_HINT = 'Save the details on the first step to unlock this step'


def build_steps(specs, active=None, *, locked_after_first=False, hint=LOCKED_HINT):
    """Return render-ready steps from `specs`, a list of dicts.

    A spec needs `key` and `label`; it may add `desc`, `done`, `locked`,
    `disabled`, `final`, `icon`, `href` and `optional`. `active` is the key
    of the current step. With `locked_after_first`, every step after the
    first is locked and disabled (a flow that has not been saved yet).
    """
    steps = []
    for index, spec in enumerate(specs):
        locked = bool(spec.get('locked')) or (locked_after_first and index > 0)
        disabled = spec.get('disabled', locked) if not (locked_after_first and index > 0) else True
        steps.append({
            'key': spec['key'],
            'label': spec['label'],
            'desc': spec.get('desc', ''),
            'number': index + 1,
            'done': bool(spec.get('done')) and not locked,
            'current': spec['key'] == active,
            'locked': locked,
            'disabled': disabled,
            'final': bool(spec.get('final')),
            'icon': spec.get('icon', ''),
            'href': spec.get('href', ''),
            'optional': bool(spec.get('optional')),
            'hint': hint if disabled else '',
        })
    return steps


def steps_from_setup(setup_steps, active, *, locked_after_first=False):
    """Steps from the `{'key', 'label', 'complete', ...}` dicts the setup
    views already compute for their panels."""
    return build_steps([
        {'key': step['key'], 'label': step['label'], 'desc': step.get('description', ''),
         'done': step['complete'], 'optional': step.get('optional', False)}
        for step in setup_steps
    ], active, locked_after_first=locked_after_first)


def unsaved_steps(labels, hint=LOCKED_HINT):
    """A flow that has not been saved yet: the first step is current and the
    rest are locked. `labels` is a list of `(key, label)` or
    `(key, label, desc)` tuples."""
    specs = [{'key': item[0], 'label': item[1], 'desc': item[2] if len(item) > 2 else ''}
             for item in labels]
    return build_steps(specs, specs[0]['key'], locked_after_first=True, hint=hint)
