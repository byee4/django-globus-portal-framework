import logging
from urllib.parse import urlparse
from collections import OrderedDict
from json import dumps
import globus_sdk
from django.contrib import messages
from django.shortcuts import render, redirect
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth import logout as django_logout

from globus_portal_framework.gclients import revoke_globus_tokens

from globus_portal_framework.apps import get_setting
from globus_portal_framework.gsearch import (get_search_query,
                                             get_search_filters)
from globus_portal_framework import (
    preview, helper_page_transfer, get_helper_page_url,
    get_subject, post_search, PreviewException, PreviewURLNotFound,
    ExpiredGlobusToken, check_exists, get_template
)

log = logging.getLogger(__name__)


def index_selection(request):
    context = {'search_indexes': get_setting('SEARCH_INDEXES')}
    return render(request, 'index-selection.html', context)


def search(request, index):
    """
    Search the 'index' with the queryparams 'q' for query, 'filter.<filter>'
    for facet-filtering, 'page' for pagination If the user visits this
    page again without a search query, we auto search for them
    again using their last query. If the user is logged in, they will
    automatically do a credentialed search for Globus Search to return
    confidential results. If more results than settings.SEARCH_RESULTS_PER_PAGE
    are returned, they are paginated (Globus Search does the pagination, we
    only do the math to calculate the offset).

    Query Params
    q: key words for the users search. Ex: 'q=foo*' will search for 'foo*'

    filter.: Filter results on facets defined in settings.SEARCH_SCHEMA. The
    syntax for filters using query params is:
    '?filter.<filter_type>=<filter_value>, where 'filter.<filter_type>' is
    defined in settings.SEARCH_SCHEMA and <filter_value> is any value returned
    by Globus Search contained within search results. For example, we can
    define a filter 'mdf.elements' in our schema, and use it to filter all
    results containing H (Hydrogen).

    page: Page of the search results. Number of results displayed per page is
    configured in settings.SEARCH_RESULTS_PER_PAGE, and number of pages can be
    controlled with settings.SEARCH_MAX_PAGES.

    Templates:
    The resulting page is templated using the 'search.html' template, and
    'search-results.html' and 'search-facets.html' template components. The
    context required for searches is shown here:

    {
        'search': {
            'facets': [
                {'buckets': [{'field_name': 'mdf.resource_type',
                            'value': 'record'}],
                'name': 'Resource Type'},
                <More Facets>...
            ],
            'pagination': {'current_page': 1, 'pages': [{'number': 1}]},
            'search_results': [
            {
                'subject': '<Globus Search Subject>',
                'fields': {
                    'titles': {'field_name': 'titles',
                                                    'value': '<Result Title>'},
                    'version': {'field_name': 'version', 'value': '0.3.2'},
                    '<field_name>': {'field_name': '<display_name>',
                                     'value': '<field_value>'},
                    'foo_field': {'field_name': 'foo', 'value': 'bar'}
                }
            }, <More Search Results>...]
        }
    }

    Example request:
    http://myhost/?q=foo*&page=2&filter.my.special.filter=goodresults
    """
    context = {}
    query = get_search_query(request)
    if query:
        filters = get_search_filters(request)
        context['search'] = post_search(index, query, filters, request.user,
                                        request.GET.get('page', 1))
        request.session['search'] = {
            'full_query': urlparse(request.get_full_path()).query,
            'query': query,
            'filters': filters,
            'index': index,
        }
        error = context['search'].get('error')
        if error:
            messages.error(request, error)
    return render(request, get_template(index, 'search.html'), context)


def search_debug(request, index):
    query = get_search_query(request)
    filters = get_search_filters(request)
    results = post_search(index, query, filters, request.user, 1)
    context = {
        'search': results,
        'facets': dumps(results['facets'], indent=2)
    }
    return render(request, get_template(index, 'search-debug.html'), context)


def search_debug_detail(request, index, subject):
    sub = get_subject(index, subject, request.user)
    debug_fields = {name: dumps(data, indent=2) for name, data in sub.items()}
    dfields = OrderedDict(debug_fields)
    dfields.move_to_end('all')
    sub['django_portal_framework_debug_fields'] = dfields
    return render(request,
                  get_template(index, 'search-debug-detail.html'), sub)


def detail(request, index, subject):
    """
    Load a page for showing details for a single search result. The data is
    exactly the same as the entries loaded by the index page in the
    'search_results'. The template is ultimately responsible for which fields
    are displayed. The only real functional difference between the index page
    and the detail page is that it displays only a single result. The
    detail-overview.html template is used to render the page.

    Example request:
    http://myhost/detail/<subject>

    Example context:
    {'subject': '<Globus Search Subject>',
     'fields': {
                'titles': {'field_name': 'titles', 'value': '<Result Title>'},
                'version': {'field_name': 'version', 'value': '0.3.2'},
                '<field_name>': {'field_name': '<display_name>', 'value':
                                                            '<field_value>'}
                }
    }
    """
    return render(request, get_template(index, 'detail-overview.html'),
                  get_subject(index, subject, request.user))


@csrf_exempt
def detail_transfer(request, index, subject):
    context = get_subject(index, subject, request.user)
    task_url = 'https://www.globus.org/app/activity/{}/overview'
    if request.user.is_authenticated:
        try:
            # Hacky, we need to formalize remote file manifests
            if 'remote_file_manifest' not in context.keys():
                raise ValueError('Please add "remote_file_manifest" to '
                                 '"fields" for {} in order to use transfer.'
                                 ''.format(index))
            elif not context.get('remote_file_manifest'):
                raise ValueError('"remote_file_manifest" not found in search '
                                 'metadata for index {}. Cannot start '
                                 'Transfer.'.format(index))
            parsed = urlparse(context['remote_file_manifest'][0]['url'])
            ep, path = parsed.netloc, parsed.path
            # Remove line in version 4 after issue #29 is resolved
            ep = ep.replace(':', '')
            check_exists(request.user, ep, path, raises=True)
            if request.method == 'POST':
                task = helper_page_transfer(request, ep, path,
                                            helper_page_is_dest=True)
                context['transfer_link'] = task_url.format(task['task_id'])
            this_url = reverse('detail-transfer', args=[index, subject])
            full_url = request.build_absolute_uri(this_url)
            # This url will serve as both the POST destination and Cancel URL
            context['helper_page_link'] = get_helper_page_url(
                full_url, full_url, folder_limit=1, file_limit=0)
        except globus_sdk.TransferAPIError as tapie:
            context['detail_error'] = tapie
            if tapie.code == 'AuthenticationFailed' \
                    and tapie.message == 'Token is not active':
                raise ExpiredGlobusToken()
            if tapie.code not in ['EndpointPermissionDenied']:
                log.error('Unexpected Error found during transfer request',
                          tapie)
        except ValueError as ve:
            log.error(ve)
    return render(request,
                  get_template(index, 'detail-transfer.html'), context)


def detail_preview(request, index, subject, endpoint=None, url_path=None):
    context = get_subject(index, subject, request.user)
    try:
        scope = request.GET.get('scope')
        if not any((endpoint, url_path, scope)):
            log.error('Preview Error: Endpoint, Path, or Scope not given. '
                      '(Got: {}, {}, {})'.format(endpoint, url_path, scope))
            raise PreviewURLNotFound(subject)
        url = 'https://{}/{}'.format(endpoint, url_path)
        log.debug('Previewing with url: {}'.format(url))
        context['preview_data'] = \
            preview(request.user, url, scope, get_setting('PREVIEW_DATA_SIZE'))
    except PreviewException as pe:
        if pe.code in ['UnexpectedError', 'ServerError']:
            log.exception(pe)
        context['detail_error'] = pe
        log.debug('User error: {}'.format(pe))
    return render(request, get_template(index, 'detail-preview.html'), context)


def logout(request, next='/'):
    """
    Revoke the users tokens and pop their Django session. Users will be
    redirected to the query parameter 'next' if it is present. If the 'next'
    query parameter 'next' is not present, the parameter next will be used
    instead.
    """
    if request.user.is_authenticated:
        revoke_globus_tokens(request.user)
        django_logout(request)
    return redirect(request.GET.get('next', next))
