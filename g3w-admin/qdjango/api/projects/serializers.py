from django.http.request import QueryDict
from django.conf import settings
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers
from rest_framework.fields import empty
from guardian.shortcuts import get_objects_for_user, get_anonymous_user
from owslib.wms import WebMapService
from qdjango.models import Project, Layer, Widget, SessionTokenFilter
from qdjango.utils.data import QGIS_LAYER_TYPE_NO_GEOM
from qdjango.utils.models import get_capabilities4layer
from qdjango.signals import load_qdjango_widget_layer
from qdjango.apps import get_qgs_project
from qdjango.utils.structure import QdjangoMetaLayer, datasourcearcgis2dict
from core.utils.structure import mapLayerAttributes
from core.configs import *
from core.signals import after_serialized_project_layer
from core.mixins.api.serializers import G3WRequestSerializer
from core.api.serializers import update_serializer_data
from core.utils.structure import RELATIONS_ONE_TO_MANY
from core.utils.qgisapi import get_qgis_layer, count_qgis_features
from core.utils.general import clean_for_json

from qgis.core import (
    QgsJsonUtils,
    QgsMapLayer,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCoordinateTransformContext,
    QgsExpression
)
from qgis.server import QgsServerProjectUtils
from qgis.PyQt.QtCore import QVariant, QDate, QDateTime, Qt

from ..utils import serialize_vectorjoin
from collections import OrderedDict
import json

import logging
logger = logging.getLogger(__name__)


class ProjectSerializer(G3WRequestSerializer, serializers.ModelSerializer):

    name = serializers.CharField(source='title', read_only=True)
    layerstree = serializers.SerializerMethodField()

    def get_layerstree(self, instance):
        """
        Return dict layertree structure from DB
        :param instance: qdjango  Project model instance.
        :return: dict
        """
        return eval(instance.layers_tree)

    def get_map_extent(self, instance):
        """
        Get init and map extent properties,from instance
        :param instance: qdjango Project model instance
        :return: tuple
        """

        extent = eval(instance.initial_extent)

        init_map_extent = [
            float(extent['xmin']),
            float(extent['ymin']),
            float(extent['xmax']),
            float(extent['ymax'])
        ]

        if instance.max_extent:
            max_extent = eval(instance.max_extent)
            map_extent = [
                float(max_extent['xmin']),
                float(max_extent['ymin']),
                float(max_extent['xmax']),
                float(max_extent['ymax'])
            ]
        else:

            # set max extent to CRS bounds
            if instance.group.srid.auth_srid == '4326':
                rectangle = QgsCoordinateReferenceSystem('EPSG:4326').bounds()
            else:

                # reproject CRS bounds
                to_srid = QgsCoordinateReferenceSystem(
                    f'EPSG:{instance.group.srid.auth_srid}')
                from_srid = QgsCoordinateReferenceSystem('EPSG:4326')
                ct = QgsCoordinateTransform(
                    from_srid, to_srid, QgsCoordinateTransformContext())
                rectangle = ct.transform(to_srid.bounds())

            map_extent = [
                rectangle.xMinimum(),
                rectangle.yMinimum(),
                rectangle.xMaximum(),
                rectangle.yMaximum()
            ]

        # if use_map_extent_as_init_extent is not flaged set init_map_extent as map_extent
        if not instance.use_map_extent_as_init_extent:
            init_map_extent = map_extent

        return init_map_extent, map_extent

    def get_map_layers_relations(self, instance):
        """
        Get qgis project layers relations
        :param instance: qdjango Project model instance
        :return: list
        """

        relations = eval(instance.relations)
        map_relations = []
        for relation in relations:
            relation['type'] = RELATIONS_ONE_TO_MANY
            map_relations.append(relation)
        return map_relations

    def get_map_layers_relations_from_vectorjoins(self, layer_id, vectorjoins, layers):
        """
        Vector joins layer from DB
        :param layer_id: qgis layer id
        :param vectorjoins: vector joins data from serilized layer
        :param layers: queryset of Layer model from Project model instance
        :return: list
        """
        joins = eval(vectorjoins)
        map_relations = []
        for n, join in enumerate(joins):
            if join['joinLayerId'] in layers:
                map_relations.append(serialize_vectorjoin(layer_id, n, join))
        return map_relations

    def _set_options(self, instance):
        """
        Se client options for services: WMS, WFS etc.
        :param instance: qdjango Project model instance
        :return: dict
        """
        options = {}

        # set context base legend
        options['context_base_legend'] = instance.context_base_legend

        # set feature_count:
        if hasattr(instance, 'feature_count_wms'):
            options['feature_count'] = instance.feature_count_wms

        # set multi layer query
        options['querymultilayers'] = []

        if hasattr(instance, 'multilayer_query') and instance.multilayer_query == 'multiple':
            options['querymultilayers'].append('query')

        if hasattr(instance, 'multilayer_querybybbox') and instance.multilayer_querybybbox == 'multiple':
            options['querymultilayers'].append('querybbox')

        if hasattr(instance, 'multilayer_querybypolygon') and instance.multilayer_querybypolygon == 'multiple':
            options['querymultilayers'].append('querybypolygon')

        # auto_zoom
        options['autozoom_query'] = getattr(instance, 'autozoom_query', False)

        # for tabs TOC
        options['toc_tab_default'] = getattr(
            instance, 'toc_tab_default', 'layers')

        # legend position
        options['legend_position'] = getattr(
            instance, 'legend_position', 'tab')

        return options

    def _set_ows_method(self, instance):
        """
        Set base http method for OWS calls
        :param instance: qdjango Project model instance
        :return: str
        """

        method = getattr(settings, 'CLIENT_OWS_METHOD', 'GET')
        return method if method in ('GET', 'POST') else 'GET'

    def get_metadata(self, instance, qgs_project):
        """Build metadata for layer by QgsProject instance
        :param instance: qdjango Layer model instance
        :param qgs_project: QgsProject instance
        :return: dict
        """

        metadata = {
            'name': 'WMS'
        }
        for service_property in [
            'Title',
            'Abstract',
            'Fees',
            'AccessConstraints',
            'Keywords',
            'OnlineResource'

        ]:
            metadata[service_property.lower()] = getattr(
                QgsServerProjectUtils, f'owsService{service_property}')(qgs_project)

        # contactinformations
        metadata.update(OrderedDict({
            'contactinformation': OrderedDict({
                'personprimary': {},
            })
        }))

        for property in ('ContactPerson', 'ContactOrganization', 'ContactPosition'):
            metadata['contactinformation']['personprimary'].update({
                property: getattr(QgsServerProjectUtils,
                                  f'owsService{property}')(qgs_project)
            })

        metadata['contactinformation'].update(OrderedDict({
            'contactvoicetelephone': QgsServerProjectUtils.owsServiceContactPhone(qgs_project),
            'contactelectronicmailaddress': QgsServerProjectUtils.owsServiceContactMail(qgs_project)
        }))

        return metadata

    def reset_filtertoken(self):
        """Check session token filter ad delete it"""

        try:
            if settings.SESSION_COOKIE_NAME in self.request.COOKIES:
                stf = SessionTokenFilter.objects.get(
                    sessionid=self.request.COOKIES[settings.SESSION_COOKIE_NAME])
                stf.delete()
        except AttributeError:
            return None
        except SessionTokenFilter.DoesNotExist:
            return None

    def layer_is_empty(self, layer):
        """
        Check if a vector layer is empty (not data)

        :param layer: qdjango Layer model instance
        :return: True or False
        :rtype: boolean
        """

        qgis_layer = get_qgis_layer(layer)
        if qgis_layer.type() == QgsMapLayer.VectorLayer:
            return not bool(count_qgis_features(qgis_layer))
        else:
            return False

    def set_map_themes(self, ret, qgs_project):
        """
        Set QGIS map themes if they are into project

        :param ret: dict to return in to_representation
        :param qgs_project: QgsProject instance
        :return: None
        """

        ret['map_themes'] = []
        map_themes = qgs_project.mapThemeCollection().mapThemes()
        if len(map_themes) == 0:
            return

        for map_theme in map_themes:
            theme = {
                'theme': map_theme,
                'styles': {}
            }

            for r in qgs_project.mapThemeCollection().mapThemeState(map_theme).layerRecords():
                theme['styles'].update({
                    r.layer().id(): r.currentStyle
                })

            ret['map_themes'].append(theme)

    def to_representation(self, instance):
        logging.warning('Serializer')
        ret = super(ProjectSerializer, self).to_representation(instance)
        logging.warning('Before reading project')

        # add a QGSMapLayer instance
        qgs_project = get_qgs_project(instance.qgis_file.path)

        logging.warning('Got project: %s' % qgs_project.fileName())

        # set init and map extent
        ret['initextent'], ret['extent'] = self.get_map_extent(instance)

        ret['print'] = json.loads(clean_for_json(
            instance.layouts)) if instance.layouts else []

        # Get layer which request.user can view:
        if self.request:
            view_layer_ids = list(
                set([l.qgs_layer_id for l in get_objects_for_user(self.request.user, 'qdjango.view_layer', Layer).
                     filter(project=instance)]).union(set(
                         [l.qgs_layer_id for l in get_objects_for_user(get_anonymous_user(), 'qdjango.view_layer', Layer).
                          filter(project=instance)]
                     ))
            )

        # add layers data, widgets
        # init properties
        if qgs_project.layerTreeRoot().hasCustomLayerOrder():
            ret['layers'] = {}
        else:
            ret['layers'] = []
        ret['search'] = []
        ret['widget'] = []
        ret['relations'] = []

        layers = {l.qgs_layer_id: l for l in instance.layer_set.all()}

        # check fo title
        if hasattr(settings, 'G3W_CLIENT_SEARCH_TITLE'):
            ret['search_title'] = _(settings.G3W_CLIENT_SEARCH_TITLE)

        # for client map like multilayer
        meta_layer = QdjangoMetaLayer()
        to_remove_from_layerstree = []

        def readLeaf(layer, container):

            if 'nodes' in layer:
                for node in layer['nodes']:
                    readLeaf(node, layer['nodes'])
            else:

                # Check for empty vector layer
                if settings.G3W_CLIENT_NOT_SHOW_EMPTY_VECTORLAYER and self.layer_is_empty(layers[layer['id']]):
                    return

                # Check if layer is visible for user
                if self.request and not layer['id'] in view_layer_ids:
                    return

                try:
                    layer_serialized = LayerSerializer(
                        layers[layer['id']], qgs_project=qgs_project, request=self.request)
                except KeyError:
                    logger.error(
                        'Layer %s is missing from QGIS project!' % layer['id'])
                    return

                layer_serialized_data = layer_serialized.data

                # alter layer serialized data from plugin
                # send layerseralized original and came back only key->value changed
                for signal_receiver, data in after_serialized_project_layer.send(layer_serialized,
                                                                                 layer=layers[layer['id']],
                                                                                 request=self.request
                                                                                 ):
                    update_serializer_data(layer_serialized_data, data)
                layer_serialized_data['multilayer'] = meta_layer.getCurrentByLayer(
                    layer_serialized_data)

                # check for vectorjoins and add to project relations
                if layer_serialized_data['vectorjoins']:
                    ret['relations'] += self.get_map_layers_relations_from_vectorjoins(
                        layer['id'],
                        layer_serialized_data['vectorjoins'],
                        layers
                    )
                del(layer_serialized_data['vectorjoins'])

                if qgs_project.layerTreeRoot().hasCustomLayerOrder():
                    ret['layers'].update(
                        {layer_serialized_data['id']: layer_serialized_data})
                else:
                    ret['layers'].append(layer_serialized_data)

                # get widgects for layer
                widgets = layers[layer['id']].widget_set.all()
                for widget in widgets:
                    widget_serializzer_data = WidgetSerializer(
                        widget, layer=layers[layer['id']]).data
                    if widget_serializzer_data['type'] in ('search', 'search_1n'):
                        widget_serializzer_data['options']['layerid'] = layer['id']
                        widget_serializzer_data['options']['querylayerid'] = layer['id']

                        ret['search'].append(widget_serializzer_data)
                    else:
                        load_qdjango_widget_layer.send(
                            self, layer=layer, ret=ret, widget=widget)

        for l in ret['layerstree']:
            readLeaf(l, ret['layerstree'])

        # remove layers from layerstree
        for to_remove in to_remove_from_layerstree:
            to_remove[0].remove(to_remove[1])

        # for custom order layer
        # ======================
        if qgs_project.layerTreeRoot().hasCustomLayerOrder():
            custom_layer_order_ids = []
            new_order = []
            meta_layer = QdjangoMetaLayer()
            for qgs_layer in qgs_project.layerTreeRoot().customLayerOrder():
                if qgs_layer.id() in ret['layers']:
                    custom_layer_order_ids.append(qgs_layer.id())
                    lsd = ret['layers'][qgs_layer.id()]
                    lsd['multilayer'] = meta_layer.getCurrentByLayer(
                        lsd)
                    new_order.append(lsd)

            # get layers not in customLayerOrder to add an the end of return layers list
            to_add_to_end = set(ret['layers'].keys()) - \
                set(custom_layer_order_ids)
            for qgs_layer_id in list(to_add_to_end):
                lsd = ret['layers'][qgs_layer_id]
                lsd['multilayer'] = meta_layer.getCurrentByLayer(
                    lsd)
                new_order.append(lsd)

            ret['layers'] = new_order

        # add baselayer default
        ret['initbaselayer'] = instance.baselayer.id if instance.baselayer else None

        # add relations if exists
        if instance.relations:
            ret['relations'] += self.get_map_layers_relations(instance)

        # add project metadata
        ret['metadata'] = self.get_metadata(instance, qgs_project)

        # set client options/actions
        ret.update(self._set_options(instance))

        # set ow method
        ret['ows_method'] = self._set_ows_method(instance)

        # add html_page_title
        ret['html_page_title'] = u'{} | {}'.format(
            getattr(settings, 'G3WSUITE_CUSTOM_TITLE', 'g3w - client'),
            instance.title_ur if instance.title_ur else instance.title)

        # set name by language if is set
        if instance.title_ur:
            ret['name'] = instance.title_ur

        # Set to macrogroup images if options use_logo is checked
        try:
            if instance.group.use_logo_client:
                ret['thumbnail'] = instance.group.header_logo_img.url
            else:
                macrogroup = instance.group.macrogroups.get(
                    use_logo_client=True)
                ret['thumbnail'] = macrogroup.logo_img.url
        except:
            pass

        ret['search_endpoint'] = settings.G3W_CLIENT_SEARCH_ENDPOINT

        # QGIS project themes
        # ----------------------------------
        self.set_map_themes(ret, qgs_project)

        # reset tokenfilter by session
        self.reset_filtertoken()

        return ret

    class Meta:
        model = Project
        fields = (
            'id',
            'name',
            'layerstree',
            'thumbnail',
            'wms_use_layer_ids',
            'qgis_version'
        )


class LayerSerializer(G3WRequestSerializer, serializers.ModelSerializer):

    id = serializers.CharField(source='qgs_layer_id')
    minscale = serializers.IntegerField(source='min_scale')
    maxscale = serializers.IntegerField(source='max_scale')
    crs = serializers.IntegerField(source='srid')
    servertype = serializers.SerializerMethodField()

    def __init__(self, instance=None, data=empty, **kwargs):

        # set QsgMapLayer instance
        self.qgs_project = kwargs['qgs_project']
        del (kwargs['qgs_project'])

        super(LayerSerializer, self).__init__(instance, data, **kwargs)

    class Meta:
        model = Layer
        fields = (
            'id',
            'name',
            'origname',
            'geometrytype',
            'crs',
            'title',
            'scalebasedvisibility',
            'minscale',
            'maxscale',
            'servertype',
            'vectorjoins',
            'exclude_from_legend',
            'not_show_attributes_table',
            'download',
            'download_xls',
            'download_gpx',
            'download_csv',
            'download_gpkg',
            'editor_form_structure',
            'styles'
        )

    def get_servertype(self, instance):
        return MSTYPES_QGIS

    def get_attributes(self, instance):
        """
        List of layer attributes(fields)
        :param instance: qdjango Layer model instance
        :return: list
        """
        columns = mapLayerAttributes(
            instance) if instance.database_columns else []

        # evaluate fields to show or not by qgis project
        column_to_exclude = eval(
            instance.exclude_attribute_wms) if instance.exclude_attribute_wms else []

        visible_columns = instance.visible_fields_for_user(self.request.user)
        for column in columns:
            column['show'] = (column['name'] in visible_columns) and (
                column['name'] not in column_to_exclude)
        return columns

    def get_ows(self, instance):
        """
        Get wfscapabilities to add wos services for layer
        :param instance: Qdjango Layer model instance
        :return: array
        """

        ret = ['WMS']
        if instance.wfscapabilities:
            ret.append('WFS')

        # TODO: add WCS if is set

        return ret

    def get_metadata(self, instance, qgs_maplayer):
        """Build metadata for layer by QgsMapLayer instance
        :param instance: qdjango Layer model instance
        :param qgs_maplayer: QgsMapLayer instance
        :return: dict
        """

        metadata = {}
        metadata['title'] = instance.title

        # try to build by qgs_layer
        metadata['attributes'] = []
        if instance.database_columns:

            for f in qgs_maplayer.fields():
                attribute = {}
                attribute['name'] = f.name()
                # attribute['editType'] = f.editType()
                attribute['typeName'] = f.typeName()
                attribute['comment'] = f.comment()
                attribute['length'] = f.length()
                attribute['precision'] = f.precision()
                attribute['type'] = QVariant.typeToName(f.type())
                attribute['alias'] = f.alias()

                metadata['attributes'].append(attribute)

        metadata['crs'] = []

        metadata['dataurl'] = {}
        if qgs_maplayer.dataUrl() != '':
            metadata['dataurl']['onlineresource'] = qgs_maplayer.dataUrl()

        metadata['metadataurl'] = {}

        if qgs_maplayer.metadataUrl() != '':
            metadata['metadataurl']['onlineresource'] = qgs_maplayer.metadataUrl()
            metadata['metadataurl']['type'] = qgs_maplayer.metadataUrlType()
            metadata['metadataurl']['format'] = qgs_maplayer.metadataUrlFormat()

        metadata['attribution'] = {}

        if qgs_maplayer.attribution() != '':
            metadata['attribution']['title'] = qgs_maplayer.attribution()

        if qgs_maplayer.attributionUrl() != '':
            metadata['attribution']['onlineresource'] = qgs_maplayer.attributionUrl()

        # add abstract
        if qgs_maplayer.abstract() != '':
            metadata['abstract'] = qgs_maplayer.abstract()

        return metadata

    def to_representation(self, instance):
        ret = super(LayerSerializer, self).to_representation(instance)

        qgs_maplayer = self.qgs_project.mapLayers()[instance.qgs_layer_id]

        # add attributes/fields
        ret['fields'] = self.get_attributes(instance)

        # add infoformat and infourl
        ret['infoformat'] = ''
        ret['infourl'] = ''

        #lidname = instance.qgs_layer_id if instance.project.wms_use_layer_ids else instance.name

        # add bbox
        if instance.geometrytype != QGIS_LAYER_TYPE_NO_GEOM:
            if instance.extent:
                ret['bbox'] = instance.extent_rect
            else:
                # get from QgsMapLayer instance
                extent = qgs_maplayer.extent()
                ret['bbox'] = {}
                ret['bbox']['minx'] = extent.xMinimum()
                ret['bbox']['miny'] = extent.yMinimum()
                ret['bbox']['maxx'] = extent.xMaximum()
                ret['bbox']['maxy'] = extent.yMaximum()

        # add capabilities
        ret['capabilities'] = get_capabilities4layer(qgs_maplayer)

        # add styles
        # FIXME: restore in the future for styles map management
        #ret['styles'] = self.qgis_projectsettings_wms.layers[lidname]['styles']

        ret['source'] = {
            'type': instance.layer_type
        }

        # add options for wms layer
        if instance.layer_type in [Layer.TYPES.wms, Layer.TYPES.arcgismapserver]:

            if instance.layer_type == Layer.TYPES.wms:
                datasource_wms = QueryDict(instance.datasource)
            else:
                datasource_wms = datasourcearcgis2dict(instance.datasource)

            if ('username' not in ret['source'] or 'password' not in ret['source']) and 'type=xyz' \
                    not in instance.datasource:

                # rebuild the dict for paramenters repeat n times i.e. 'layers' and 'styles'
                if isinstance(datasource_wms, QueryDict):
                    for p in datasource_wms.lists():
                        if p[0] in ('layers', 'styles'):
                            ret['source'].update({p[0]: ','.join(p[1])})
                        else:
                            ret['source'].update({p[0]: datasource_wms[p[0]]})
                else:
                    ret['source'].update(datasource_wms)

            ret['source']['external'] = instance.external
            if instance.external and instance.layer_type == Layer.TYPES.wms:
                try:
                    wms = WebMapService(ret['source']['url'], version='1.3.0')
                    format_options = wms.getOperationByName('GetFeatureInfo').formatOptions
                    if format_options:

                        # Filter format by supported by G3W-CLIENT
                        formats = list(set(format_options).intersection(set(settings.EXTERNAL_WMS_INFOFORMATS_SUPPORTED)))
                        if formats:
                            ret['infoformat'] = formats[0]
                            ret['infoformats'] = formats
                except Exception as e:
                    logger.debug(f'WMS layer GetFeatureInfo formats available: {e}')



        # replace crs property if is not none with dict structure

        if ret['crs']:
            crs = QgsCoordinateReferenceSystem(f'EPSG:{ret["crs"]}')

            # Patch for Proj4 > 4.9.3 version
            if ret["crs"] == 3003:
                proj4 = "+proj=tmerc +lat_0=0 +lon_0=9 +k=0.9996 +x_0=1500000 +y_0=0 +ellps=intl " \
                        "+towgs84=-104.1,-49.1,-9.9,0.971,-2.917,0.714,-11.68 +units=m +no_defs"
            else:
                proj4 = crs.toProj4()

            ret['crs'] = {
                'epsg': crs.postgisSrid(),
                'proj4': proj4,
                'geographic': crs.isGeographic(),
                'axisinverted': crs.hasAxisInverted()
            }

        # add metadata
        ret['metadata'] = self.get_metadata(instance, qgs_maplayer)

        # eval editor_form_structure
        if ret['editor_form_structure']:
            ret['editor_form_structure'] = eval(instance.editor_form_structure)

        # add ows
        ret['ows'] = self.get_ows(instance)

        # For temporal properties
        if instance.temporal_properties:
            ret['qtimeseries'] = json.loads(instance.temporal_properties)

            if ret['qtimeseries'] and ret['qtimeseries']['mode'] == 'FeatureDateTimeInstantFromField':

                # Add start_date end end_date:
                findex = qgs_maplayer.dataProvider().fieldNameIndex(ret['qtimeseries']['field'])

                ret['qtimeseries']['start_date'] = qgs_maplayer.minimumValue(findex)
                ret['qtimeseries']['end_date'] = qgs_maplayer.maximumValue(findex)
                if isinstance(ret['qtimeseries']['start_date'], QDate) or isinstance(ret['qtimeseries']['start_date'], QDateTime):
                    if not hasattr(QDate, 'isoformat'):
                        QDate.isoformat = lambda d: d.toString(Qt.ISODate)
                    if not hasattr(QDateTime, 'isoformat'):
                        QDateTime.isoformat = lambda d: d.toString(Qt.ISODateWithMs)
                    ret['qtimeseries']['start_date'] = ret['qtimeseries']['start_date'].isoformat()
                    ret['qtimeseries']['end_date'] = ret['qtimeseries']['end_date'].isoformat()


        return ret


class WidgetSerializer(serializers.ModelSerializer):
    """
    Serializzer for Qdjango Widget
    """

    def __init__(self, instance=None, data=empty, **kwargs):
        self.layer = kwargs['layer']
        del(kwargs['layer'])
        super(WidgetSerializer, self).__init__(instance, data, **kwargs)

    def to_representation(self, instance):
        ret = super(WidgetSerializer, self).to_representation(instance)
        ret['type'] = instance.widget_type

        # get edittype
        edittypes = eval(self.layer.edittypes)

        if ret['type'] == 'search':
            body = json.loads(instance.body)

            ret['options'] = {
                'queryurl': None,
                'title': body['title'],
                'results': body['results'],
                'filter': [],
                'dozoomtoextent': body['dozoomtoextent'],
                # 'zoom': body['zoom']
            }

            # other layers
            if 'otherlayers' in body:
                ret['options'].update({
                    'otherquerylayerids': body['otherlayers']
                })

            for field in body['fields']:

                # if widgettype is selectbox, get values
                if 'widgettype' in field and field['widgettype'] == 'selectbox':

                    field['input']['type'] = 'selectfield'
                    field['input']['options']['values'] = []

                    edittype = edittypes[field['name']]

                    # check if field has a widget edit type
                    widget_type = edittype['widgetv2type']
                    if field['name'] in edittypes and widget_type in ('ValueMap', 'ValueRelation'):
                        if widget_type == 'ValueMap':
                            field['input']['options']['values'] = edittype['values']
                        else:

                            # Add layer params
                            field['input']['options']['key'] = edittype['Value']
                            field['input']['options']['value'] = edittype['Key']
                            field['input']['options']['layer_id'] = edittype['Layer']

                # For AutoccOmpleteBox imput type
                if 'widgettype' in field and field['widgettype'] == 'autocompletebox':
                    field['input']['type'] = 'autocompletefield'

                input = field['input']
                input['options']['blanktext'] = field['blanktext']
                ret['options']['filter'].append({
                    'op': field['filterop'],
                    'attribute': field['name'],
                    'label': field['label'],
                    'input': input,
                    'logicop': field.get('logicop', 'AND').upper()
                })

            # rewrite type if relations is set relations
            if 'relations' in body and body['relations'] != '':
                ret['type'] = 'search_1n'
                ret['options']['search_1n_relationid'] = body['relations']
                del(body['relations'])

        else:
            ret['body'] = json.loads(instance.body)
        return ret

    class Meta:
        model = Widget
        fields = (
            'id',
            'name',
        )
