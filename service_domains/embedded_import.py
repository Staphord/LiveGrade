"""Shared preview-first spreadsheet import for setup steps.

Each import supplies `parse`, `plan` and `apply`; this module owns the signed
preview token, the owner scoping and the preview/apply/cancel flow, so every
setup step imports the same way. A plan is a dict with `columns`, `rows`
(`row`, `cells`, `errors`), `errors` (row number or None, message), `ok` and
`summary`.
"""
from django.contrib import messages
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404

from .forms import ApplyPreviewForm

TOKEN_MAX_AGE = 15 * 60
MAX_TOKEN_LENGTH = 150_000


def _salt(kind):
    return f'livegrade.embedded.import.{kind}.v1'


def _preview_key(kind, owner):
    return f'embedded_import_preview_{kind}_{owner.pk}'


def _errors_key(kind, owner):
    return f'embedded_import_errors_{kind}_{owner.pk}'


def _payload(request, owner, **extra):
    return {'owner': owner.pk, 'organization': request.organization['id'],
            'user': request.user.pk, **extra}


def _belongs(request, owner, payload):
    return (isinstance(payload, dict) and payload.get('owner') == owner.pk
            and payload.get('organization') == request.organization['id']
            and payload.get('user') == request.user.pk)


def load_preview(request, kind, owner, plan):
    """The stored preview's plan and token, or (None, '') when absent or stale."""
    key = _preview_key(kind, owner)
    raw = request.session.get(key, '')
    if not raw or len(raw) > MAX_TOKEN_LENGTH:
        request.session.pop(key, None)
        return None, ''
    try:
        preview = signing.loads(raw, salt=_salt(kind), max_age=TOKEN_MAX_AGE)
    except (signing.BadSignature, signing.SignatureExpired, TypeError, ValueError):
        request.session.pop(key, None)
        return None, ''
    if not _belongs(request, owner, preview) or not isinstance(preview.get('parsed'), dict):
        request.session.pop(key, None)
        return None, ''
    result = plan(owner, preview['parsed'])
    return result, (raw if result['ok'] else '')


def take_errors(request, kind, owner):
    """One-time feedback for the next page load, only for the same owner."""
    payload = request.session.pop(_errors_key(kind, owner), None)
    if not _belongs(request, owner, payload):
        return []
    errors = payload.get('errors')
    return [item for item in errors if isinstance(item, str)] if isinstance(errors, list) else []


def _remember_errors(request, kind, owner, errors):
    request.session[_errors_key(kind, owner)] = _payload(
        request, owner, errors=errors or ['The spreadsheet could not be processed. Try again.'])


def _form_errors(form):
    return [str(error) for field_errors in form.errors.values() for error in field_errors]


def handle_post(request, *, kind, owner, form_class, parse, plan, apply, success, back):
    """Preview, apply or cancel an import and return to the setup step."""
    request.session.pop(_errors_key(kind, owner), None)
    action = request.POST.get('action')
    if action == 'cancel':
        request.session.pop(_preview_key(kind, owner), None)
        return back()
    if action == 'apply':
        form = ApplyPreviewForm(request.POST)
        errors = _form_errors(form)
        preview = None
        if form.is_valid() and len(form.cleaned_data['preview_token']) <= MAX_TOKEN_LENGTH:
            try:
                preview = signing.loads(form.cleaned_data['preview_token'], salt=_salt(kind),
                                        max_age=TOKEN_MAX_AGE)
            except (signing.BadSignature, signing.SignatureExpired, TypeError, ValueError):
                errors = ['This preview expired or is invalid. Upload the spreadsheet again.']
        if preview is not None:
            if not _belongs(request, owner, preview) or not isinstance(preview.get('parsed'), dict):
                raise Http404
            try:
                apply(owner, preview['parsed'], organization=request.organization, user=request.user)
            except PermissionDenied:
                raise Http404
            except ValidationError as exc:
                errors = exc.messages
            else:
                request.session.pop(_preview_key(kind, owner), None)
                messages.success(request, success)
                return back()
        _remember_errors(request, kind, owner, errors)
        return back()
    request.session.pop(_preview_key(kind, owner), None)
    form = form_class(request.POST, request.FILES)
    if not form.is_valid():
        _remember_errors(request, kind, owner, _form_errors(form))
        return back()
    parsed = parse(form.cleaned_data['file'])
    request.session[_preview_key(kind, owner)] = signing.dumps(
        _payload(request, owner, parsed=parsed), salt=_salt(kind), compress=True)
    return back()
