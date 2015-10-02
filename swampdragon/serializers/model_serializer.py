from django.core.exceptions import ValidationError
from django.db.models.fields.related import ReverseSingleRelatedObjectDescriptor, ForeignRelatedObjectsDescriptor, \
    ReverseManyRelatedObjectsDescriptor, ManyRelatedObjectsDescriptor
from swampdragon.model_tools import get_property, get_model
from swampdragon.serializers.field_serializers import serialize_field
from swampdragon.serializers.serializer import Serializer
from swampdragon.serializers.serializer_importer import get_serializer
from swampdragon.serializers.field_deserializers import get_deserializer
from swampdragon.serializers.serializer_tools import get_id_mappings
from swampdragon.serializers.validation import ModelValidationError


class ModelSerializerMeta(object):
    def __init__(self, options):
        self.model = get_model(getattr(options, 'model'))
        self.publish_fields = set(getattr(options, 'publish_fields', None) or self.get_fields(self.model))
        self.always_publish_fields = set(getattr(options, 'always_publish_fields', None) or [])
        self.never_publish_fields = set(getattr(options, 'never_publish_fields', None) or [])
        self.update_fields = getattr(options, 'update_fields', ())
        self.id_field = getattr(options, 'id_field', 'pk')
        self.base_channel = getattr(options, 'base_channel', self.model._meta.model_name)

    def get_fields(self, model):
        fields = []
        for f in model._meta.get_all_field_names():
            field = model._meta.get_field_by_name(f)[0]
            if hasattr(field, 'get_accessor_name'):
                fields.append(field.get_accessor_name())
            else:
                fields.append(field.name)
        return fields


class ModelSerializer(Serializer):
    def __init__(self, data=None, instance=None, initial=None):
        if data and not isinstance(data, dict):
            raise Exception('data needs to be a dictionary')
        self.opts = ModelSerializerMeta(self.Meta)
        self._instance = instance
        self._data = data
        self.initial = initial or {}
        self.base_fields = self._get_base_fields()
        self.m2m_fields = self._get_m2m_fields()
        self.related_fields = self._get_related_fields()
        self.errors = None
        self._validated = False

    class Meta(object):
        pass

    @property
    def instance(self):
        return self._instance

    def _get_base_fields(self):
        return [f.name for f in self.opts.model._meta.fields]

    def _get_related_fields(self):
        return [f for f in self.opts.update_fields if f not in self.base_fields and f not in self.m2m_fields]

    def _get_m2m_fields(self):
        related_m2m = [f.get_accessor_name() for f in self.opts.model._meta.get_all_related_many_to_many_objects()]
        m2m_fields = [f.name for f in self.opts.model._meta.local_many_to_many]
        m2m = m2m_fields + related_m2m
        return [f for f in self.opts.update_fields if f in m2m]

    def deserialize(self):
        # Set initial data
        if not self._instance:
            self._instance = self.opts.model()

        for key, val in self.initial.items():
            setattr(self.instance, key, val)

        # Deserialize base fields
        for key, val in self._data.items():
            if key not in self.opts.update_fields or key not in self.base_fields:
                continue
            try:
                self.validate_field(key, val, self._data)
                self._deserialize_field(key, val)
            except ModelValidationError as err:
                if self.errors is None:
                    self.errors = {}
                self.errors.update(err.get_error_dict())

        return self.instance

    def is_valid(self):
        self.deserialize()
        try:
            self.instance.clean_fields()
        except ValidationError as e:
            self.errors = e.message_dict
        self._validated = True
        return self.errors is None

    def save(self):
        if not self._validated:
            self.is_valid()
        if self.errors:
            raise ModelValidationError(self.errors)
        self.instance.save()

        # Serialize related fields
        for key, val in self._data.items():
            if key not in self.related_fields:
                continue
            self._deserialize_related(key, val)

        # Serialize m2m fields
        for key, val in self._data.items():
            if key not in self.m2m_fields:
                continue
            self._deserialize_related(key, val, save_instance=True)
        return self.instance

    def _deserialize_field(self, key, val):
        if hasattr(self, key):
            serializer = self._get_related_serializer(key)
            value = serializer(val).save()
            setattr(self.instance, key, value)
            value.save()
            return

        field = self.opts.model._meta.get_field(key)
        field_type = field.__class__.__name__
        deserializer = get_deserializer(field_type)
        if deserializer:
            deserializer(self.instance, key, val)
        else:
            setattr(self.instance, key, val)

    def _deserialize_related(self, key, val, save_instance=False):
        serializer = self._get_related_serializer(key)
        if isinstance(val, list):
            for v in val:
                related_instance = serializer(v).deserialize()
                if save_instance:
                    related_instance.save()
                getattr(self.instance, key).add(related_instance)
        else:
            if serializer:
                related_instance = serializer(val).deserialize()
                setattr(self.instance, key, related_instance)
            else:
                setattr(self.instance, key, val)

    def _get_related_serializer(self, key):
        serializer = getattr(self, key, None)
        if isinstance(serializer, str):
            return get_serializer(serializer, self.__class__)
        return serializer

    def get_object_map_data(self):
        return {
            'id': getattr(self.instance, self.opts.id_field),
            '_type': self.opts.model._meta.model_name
        }

    def serialize(self, fields=None, ignore_serializers=None):
        if not self.instance:
            return None

        if not fields:
            fields = self.opts.publish_fields
        else:
            fields = set(fields)

        data = self.get_object_map_data()

        # Set all the ids for related models
        # so the datamapper can find the connection
        data.update(get_id_mappings(self))

        # Serialize the fields
        for field in (fields | self.opts.always_publish_fields) - self.opts.never_publish_fields:
            data[field] = self._serialize_value(field, ignore_serializers)

        custom_serializer_functions = self._get_custom_field_serializers()
        for custom_function, name in custom_serializer_functions:
            if name in self.opts.never_publish_fields:
                continue
            serializer = getattr(self, name, None)
            if serializer:
                serializer = get_serializer(serializer, self)
                data[name] = custom_function(self.instance, serializer)
            else:
                data[name] = custom_function(self.instance)

        return data

    def _serialize_value(self, attr_name, ignore_serializers=None):
        obj_serializer = self._get_related_serializer(attr_name)
        # To prevent infinite recursion, allow serializers to be ignored
        if ignore_serializers and obj_serializer in ignore_serializers:
            return None

        val = get_property(self.instance, attr_name)

        # If we have one or more related models
        if obj_serializer and hasattr(val, 'all'):
            return [obj_serializer(instance=o).serialize(ignore_serializers=[self.__class__]) for o in val.all()]
        elif obj_serializer:
            return obj_serializer(instance=val).serialize(ignore_serializers=[self.__class__])
        elif hasattr(self.opts.model, attr_name):
            # Check if the field is a relation of any kind
            field_type = getattr(self.opts.model, attr_name)
            # Reverse FK
            if isinstance(field_type, ReverseSingleRelatedObjectDescriptor):
                rel = get_property(self.instance, attr_name)
                if rel:
                    val = rel.pk
            # FK
            elif isinstance(field_type, ForeignRelatedObjectsDescriptor):
                val = list(get_property(self.instance, attr_name).all().values_list('pk', flat=True))
            elif isinstance(field_type, ReverseManyRelatedObjectsDescriptor):
                val = list(get_property(self.instance, attr_name).all().values_list('pk', flat=True))
            elif isinstance(field_type, ManyRelatedObjectsDescriptor):
                val = list(get_property(self.instance, attr_name).all().values_list('pk', flat=True))

        # Serialize the field
        return self._serialize_field(val)

    def _serialize_field(self, val):
        # subclass as needed
        return serialize_field(val)

    @classmethod
    def get_base_channel(cls):
        if hasattr(cls.Meta, 'base_channel'):
            return '{}|'.format(getattr(cls.Meta, 'base_channel'))
        return '{}|'.format(get_model(cls.Meta.model)._meta.model_name)

    @classmethod
    def get_related_serializers(cls):
        possible_serializers = [k for k in cls.__dict__.keys() if not k.startswith('_') and not k == 'Meta']
        serializers = []
        for possible_serializer in possible_serializers:
            val = getattr(cls, possible_serializer)
            if isinstance(val, str):
                val = get_serializer(val, cls)
            if hasattr(val, 'serialize'):
                serializers.append((val, possible_serializer))
        return serializers
