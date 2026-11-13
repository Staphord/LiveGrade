from django import forms


def apply_form_field_styles(fields):
    """Assign design system classes to form field widgets."""
    for name, field in fields.items():
        widget = field.widget
        existing_class = widget.attrs.get('class', '')
        classes = set(existing_class.split()) if existing_class else set()
        if isinstance(widget, (forms.CheckboxInput, forms.RadioSelect)):
            classes.add('form-check-input')
        elif isinstance(widget, forms.Select):
            classes.add('form-select')
        elif isinstance(widget, forms.FileInput):
            classes.add('form-control')
        else:
            classes.add('form-control')
        widget.attrs['class'] = ' '.join(sorted(classes))
        if isinstance(widget, forms.NumberInput) and 'inputmode' not in widget.attrs:
            widget.attrs['inputmode'] = 'decimal' if getattr(field, 'decimal_places', 0) else 'numeric'
        if getattr(field, 'help_text', '') and 'aria-describedby' not in widget.attrs:
            widget.attrs['aria-describedby'] = f'id_{name}_helptext'


class StyledFormMixin:
    """Ensure all form widgets inherit design system classes."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_form_field_styles(self.fields)

    def __getitem__(self, name):
        bound = super().__getitem__(name)
        described_by = [identifier for identifier in
            bound.field.widget.attrs.get('aria-describedby', '').split()
            if identifier != f'id_{name}_helptext']
        if bound.help_text and bound.auto_id:
            described_by.append(f'{bound.auto_id}_helptext')
        field_name = self.add_prefix(name)
        if (self._errors is not None and self._errors.get(field_name)
                and bound.auto_id):
            described_by.append(f'{bound.auto_id}_inline_message')
        if described_by:
            bound.field.widget.attrs['aria-describedby'] = ' '.join(dict.fromkeys(described_by))
        return bound

