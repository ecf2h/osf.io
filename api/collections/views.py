from modularodm import Q
from rest_framework import generics, permissions as drf_permissions
from rest_framework.exceptions import ValidationError, NotFound

from framework.auth.core import Auth
from framework.auth.oauth_scopes import CoreScopes

from api.base import permissions as base_permissions
from api.base.filters import ODMFilterMixin
from api.base.utils import get_object_or_error
from api.collections.serializers import (
    CollectionSerializer,
    CollectionDetailSerializer,
    CollectionNodeLinkSerializer,
)

from api.nodes.permissions import (
    ContributorOrPublic,
    ReadOnlyIfRegistration,
    ContributorOrPublicForPointers,
)

from website.exceptions import NodeStateError
from website.models import Node, Pointer


class CollectionMixin(object):
    """Mixin with convenience methods for retrieving the current collection based on the
    current URL. By default, fetches the current node based on the collection_id kwarg.
    """

    serializer_class = CollectionSerializer
    node_lookup_url_kwarg = 'collection_id'

    def get_node(self, check_object_permissions=True):
        node = get_object_or_error(
            Node,
            self.kwargs[self.node_lookup_url_kwarg],
            display_name='collection'
        )
        # Nodes that are folders/collections are treated as a separate resource, so if the client
        # requests a non-collection through a collection endpoint, we return a 404
        if not node.is_folder:
            raise NotFound
        # May raise a permission denied
        if check_object_permissions:
            self.check_object_permissions(self.request, node)
        return node


class CollectionList(generics.ListCreateAPIView, ODMFilterMixin):
    """Organizer Collections organize projects and components. *Writeable*.

    Paginated list of Project Organizer Collections ordered by their `date_modified`.
    Each resource contains the full representation of the project organizer collection, meaning additional
    requests to an individual Organizer Collection's detail view are not necessary.

    The Project Organizer is a tool to allow the user to make Collections of projects, components, and registrations
    for whatever purpose the user might want to organize them. They make node_links to any Node that a user has
    read access to. Collections through this API do not nest. Currently Collections are private to any individual user,
    though that could change one day.

    ##Collection Attributes

    OSF Organizer Collection entities have the "nodes" `type`.

        name           type               description
        ---------------------------------------------------------------------------------
        title          string             title of Organizer Collection
        date_created   iso8601 timestamp  timestamp that the collection was created
        date_modified  iso8601 timestamp  timestamp when the collection was last updated


    ##Links

    See the [JSON-API spec regarding pagination](http://jsonapi.org/format/1.0/#fetching-pagination).

    ##Actions

    ###Creating New Organizer Collections

        Method:        POST
        URL:           links.self
        Query Params:  <none>
        Body (JSON):   {
                         "data": {
                           "type": "collections", # required
                           "attributes": {
                             "title":       {title},          # required
                           }
                         }
                        }
        Success:       201 CREATED + collection representation

    New Organizer Collections are created by issuing a POST request to this endpoint.  The `title` field is
    mandatory. All other fields not listed above will be ignored.  If the Organizer Collection creation is successful
    the API will return a 201 response with the representation of the new node in the body.
    For the new Collection's canonical URL, see the `links.self` field of the response.

    ##Query Params

    + `page=<Int>` -- page number of results to view, default 1

    + `filter[<fieldname>]=<Str>` -- fields and values to filter the search results on.

    Organizer Collections may be filtered by their `title`, which is a string field and will be filtered using simple
    substring matching.

    #This Request/Response

    """
    permission_classes = (
        drf_permissions.IsAuthenticatedOrReadOnly,
        base_permissions.TokenHasScope,
    )

    required_read_scopes = [CoreScopes.ORGANIZER_COLLECTIONS_BASE_READ]
    required_write_scopes = [CoreScopes.ORGANIZER_COLLECTIONS_BASE_WRITE]

    serializer_class = CollectionSerializer

    ordering = ('-date_modified', )  # default ordering

    # overrides ODMFilterMixin
    def get_default_odm_query(self):
        base_query = (
            Q('is_deleted', 'ne', True) &
            Q('is_folder', 'eq', True)
        )
        user = self.request.user
        permission_query = Q('is_public', 'eq', True)
        if not user.is_anonymous():
            permission_query = (Q('is_public', 'eq', True) | Q('contributors', 'icontains', user._id))

        query = base_query & permission_query
        return query

    # overrides ListCreateAPIView
    def get_queryset(self):
        query = self.get_query_from_request()
        return Node.find(query)

    # overrides ListCreateAPIView
    def perform_create(self, serializer):
        """Create a node.

        :param serializer:
        """
        # On creation, make sure that current user is the creator
        user = self.request.user
        serializer.save(creator=user)


class CollectionDetail(generics.RetrieveUpdateDestroyAPIView, CollectionMixin):
    """Details about Organizer Collections. *Writeable*.

    Paginated list of Project Organizer Collections ordered by their `date_modified`.
    Each resource contains the full representation of the project organizer collection, meaning additional
    requests to an individual Organizer Collection's detail view are not necessary.

    The Project Organizer is a tool to allow the user to make Collections of projects, components, and registrations
    for whatever purpose the user might want to organize them. They make node_links to any Node that a user has
    read access to. Collections through this API do not nest. Currently Collections are private to any individual user,
    though that could change one day.

    ##Collection Attributes

    OSF Organizer Collection entities have the "nodes" `type`.

        name           type               description
        ---------------------------------------------------------------------------------
        title          string             title of Organizer Collection
        date_created   iso8601 timestamp  timestamp that the collection was created
        date_modified  iso8601 timestamp  timestamp when the collection was last updated

    ##Relationships

    ###Node links

    Node links are pointers or aliases to nodes. This relationship lists all of the nodes that the Organizer Collection
    is pointing to. New node links can be created with this collection.

    ##Links

        self:  the canonical api endpoint of this node
        html:  this node's page on the OSF website

    ##Actions

    ###Update

        Method:        PUT / PATCH
        URL:           links.self
        Query Params:  <none>
        Body (JSON):   {
                         "data": {
                           "type": "nodes",   # required
                           "id":   {node_id}, # required
                           "attributes": {
                             "title":       {title},          # mandatory
                           }
                         }
                       }
        Success:       200 OK + node representation

    To update an Organizer Collection, issue either a PUT or a PATCH request against the `links.self` URL.
    The `title` and `category` fields are mandatory if you PUT and optional if you PATCH.  The `tags` parameter must be an array of strings.
    Non-string values will be accepted and stringified, but we make no promises about the stringification output.  So
    don't do that.

    ###Delete

        Method:   DELETE
        URL:      links.self
        Params:   <none>
        Success:  204 No Content

    To delete a node, issue a DELETE request against `links.self`.  A successful delete will return a 204 No Content
    response. Attempting to delete a node you do not own will result in a 403 Forbidden.

    ##Query Params

    *None*.

    #This Request/Response

    """
    permission_classes = (
        drf_permissions.IsAuthenticatedOrReadOnly,
        ContributorOrPublic,
        base_permissions.TokenHasScope,
    )

    required_read_scopes = [CoreScopes.ORGANIZER_COLLECTIONS_BASE_READ]
    required_write_scopes = [CoreScopes.ORGANIZER_COLLECTIONS_BASE_WRITE]

    serializer_class = CollectionDetailSerializer

    # overrides RetrieveUpdateDestroyAPIView
    def get_object(self):
        return self.get_node()

    # overrides RetrieveUpdateDestroyAPIView
    def perform_destroy(self, instance):
        user = self.request.user
        auth = Auth(user)
        node = self.get_object()
        try:
            node.remove_node(auth=auth)
        except NodeStateError as err:
            raise ValidationError(err.message)
        node.save()


class NodeLinksList(generics.ListCreateAPIView, CollectionMixin):
    """Node Links to other nodes. *Writeable*.

    Node Links act as pointers to other nodes. Unlike Forks, they are not copies of nodes;
    Node Links are a direct reference to the node that they point to.

    ##Node Link Attributes

    *None*

    ##Links

    See the [JSON-API spec regarding pagination](http://jsonapi.org/format/1.0/#fetching-pagination).

    ##Actions

    ###Create
        Method:        POST
        URL:           links.self
        Query Params:  <none>
        Body (JSON):   {
                         "data": {
                           "type": "node_links", # required
                         },
                         'relationships': {
                            'target_node': {
                                'data': {
                                    'type': 'nodes',
                                    'id': '<node_id>'
                                }
                            }
                        }


    ##Query Params

    + `page=<Int>` -- page number of results to view, default 1

    + `filter[<fieldname>]=<Str>` -- fields and values to filter the search results on.

    #This Request/Response
    """
    permission_classes = (
        drf_permissions.IsAuthenticatedOrReadOnly,
        ContributorOrPublic,
        ReadOnlyIfRegistration,
        base_permissions.TokenHasScope,
    )

    required_read_scopes = [CoreScopes.NODE_LINKS_READ]
    required_write_scopes = [CoreScopes.NODE_LINKS_WRITE]

    serializer_class = CollectionNodeLinkSerializer

    def get_queryset(self):
        return [
            pointer for pointer in
            self.get_node().nodes_pointer
            if not pointer.node.is_deleted
        ]


class NodeLinksDetail(generics.RetrieveDestroyAPIView, CollectionMixin):
    """Node Link details. *Writeable*.

    Node Links act as pointers to other nodes. Unlike Forks, they are not copies of nodes;
    Node Links are a direct reference to the node that they point to.

    ##Attributes

    *None*

    ##Relationships

    ##Links

    self:  the canonical api endpoint of this node

    ##Actions

    ###Delete

        Method:   DELETE
        URL:      links.self
        Params:   <none>
        Success:  204 No Content

    To delete a node_link, issue a DELETE request against `links.self`.  A successful delete will return a 204 No Content
    response. Attempting to delete a node you do not own will result in a 403 Forbidden.

    ##Query Params

    *None*.

    #This Request/Response
    """
    permission_classes = (
        ContributorOrPublicForPointers,
        drf_permissions.IsAuthenticatedOrReadOnly,
        base_permissions.TokenHasScope,
        ReadOnlyIfRegistration,
    )

    required_read_scopes = [CoreScopes.NODE_LINKS_READ]
    required_write_scopes = [CoreScopes.NODE_LINKS_WRITE]

    serializer_class = CollectionNodeLinkSerializer

    # overrides RetrieveAPIView
    def get_object(self):
        node_link_lookup_url_kwarg = 'node_link_id'
        node_link = get_object_or_error(
            Pointer,
            self.kwargs[node_link_lookup_url_kwarg],
            'node link'
        )
        # May raise a permission denied
        self.kwargs['node_id'] = self.kwargs['collection_id']
        self.check_object_permissions(self.request, node_link)
        return node_link

    # overrides DestroyAPIView
    def perform_destroy(self, instance):
        user = self.request.user
        auth = Auth(user)
        node = self.get_node()
        pointer = self.get_object()
        try:
            node.rm_pointer(pointer, auth=auth)
        except ValueError as err:  # pointer doesn't belong to node
            raise ValidationError(err.message)
        node.save()
