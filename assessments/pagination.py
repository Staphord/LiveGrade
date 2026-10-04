"""Shared pagination helper — the table convention from
integrations/views.py:integration_deliveries, applied consistently across
every primary listing in this app (roster, participation, results).
"""

from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator

PAGE_SIZE = 25
PAGE_SIZES = [25, 50, 100]


def paginate(request, queryset_or_list):
    try:
        per_page = int(request.GET.get('per_page') or PAGE_SIZE)
    except (TypeError, ValueError):
        per_page = PAGE_SIZE
    if per_page not in PAGE_SIZES:
        per_page = PAGE_SIZE

    paginator = Paginator(queryset_or_list, per_page)
    try:
        page = paginator.page(int(request.GET.get('page', 1)))
    except (PageNotAnInteger, ValueError, TypeError):
        page = paginator.page(1)
    except EmptyPage:
        page = paginator.page(paginator.num_pages)
    return paginator, page
