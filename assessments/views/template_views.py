"""Owner-only reusable setup workflows. No template grants oversight access."""
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from accounts.access import lecturer_required

from ..forms import SaveSessionTemplateForm
from ..models import AssessmentSession
from ..session_templates import save_session_template, starters_for, template_queryset
from .lecturer_views import _session

__all__ = ['session_template_list', 'session_template_save', 'session_template_delete']


@lecturer_required
@require_http_methods(['GET'])
def session_template_list(request):
    return render(request, 'assessments/template_list.html', {
        'saved_templates': template_queryset(request).prefetch_related('rubric_categories__level_descriptors'),
        'starter_templates': starters_for(getattr(request, 'service', None)),
    })


@lecturer_required
@require_http_methods(['GET', 'POST'])
def session_template_save(request, pk):
    source = _session(request, pk)
    form = SaveSessionTemplateForm(request.POST if request.method == 'POST' else None,
                                   initial={'name': source.name})
    if request.method == 'POST' and form.is_valid():
        scoped = (AssessmentSession.objects.for_request(request)
                  .for_service(getattr(request, 'service', None)).owned_by(request.user))
        try:
            save_session_template(scoped, source.pk, owner=request.user, **form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, ' '.join(error.messages))
        else:
            messages.success(request, 'Template saved. It is ready to use for a new session.')
            return redirect('assessment_template_list')
    return render(request, 'assessments/template_save.html', {'session': source, 'form': form})


@lecturer_required
@require_POST
def session_template_delete(request, template_pk):
    template = get_object_or_404(template_queryset(request), pk=template_pk)
    template.delete()
    messages.success(request, 'Template deleted. Sessions created from it are unchanged.')
    return redirect('assessment_template_list')
