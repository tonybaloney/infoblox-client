# Copyright 2015 Infoblox Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import six

from oslo_log import log as logging

from infoblox_client import exceptions as ib_ex
from infoblox_client import utils as ib_utils

LOG = logging.getLogger(__name__)


class BaseObject(object):
    """Base class that provides minimal new object model interface

    This class add next features to objects:
    - initialize public instance variables with None for fields
     defined in '_fields' and '_shadow_fields'
    - accept fields from '_fields' and '_shadow_fields' as a parameter on init
    - dynamically remap one fields into another using _remap dict,
     mapping is in effect on all stages (on init, getter and setter)
    - provides nice object representation that contains class
     and not None object fields (useful in python interpretter)
    """
    _fields = []
    _shadow_fields = []
    _remap = {}
    _infoblox_type = None

    def __init__(self, **kwargs):
        mapped_args = self._remap_fields(kwargs)
        for field in self._fields + self._shadow_fields:
            if field in mapped_args:
                setattr(self, field, mapped_args[field])
            else:
                # Init all not initialized fields with None
                if not hasattr(self, field):
                    setattr(self, field, None)

    def __getattr__(self, name):
        # Map aliases into real fields
        if name in self._remap:
            return getattr(self, self._remap[name])
        else:
            # Default behaviour
            raise AttributeError

    def __setattr__(self, name, value):
        if name in self._remap:
            return setattr(self, self._remap[name], value)
        else:
            super(BaseObject, self).__setattr__(name, value)

    def __repr__(self):
        data = {field: getattr(self, field)
                for field in self._fields + self._shadow_fields
                if getattr(self, field) is not None}
        data_str = ', '.join("{0}={1}".format(key, data[key]) for key in data)
        return "{0}: {1}".format(self.__class__.__name__, data_str)

    @classmethod
    def _remap_fields(cls, kwargs):
        """Map fields from kwargs into dict acceptable by NIOS"""
        mapped = {}
        for key in kwargs:
            if key in cls._remap:
                mapped[cls._remap[key]] = kwargs[key]
            elif key in cls._fields or key in cls._shadow_fields:
                mapped[key] = kwargs[key]
            else:
                raise ValueError("Unknown parameter %s for class %s" %
                                 (key, cls))
        return mapped

    @classmethod
    def from_dict(cls, ip_dict):
        return cls(**ip_dict)

    def to_dict(self):
        return {field: getattr(self, field) for field in self._fields
                if getattr(self, field, None) is not None}

    @property
    def ref(self):
        if hasattr(self, '_ref'):
            return self._ref


class InfobloxObject(BaseObject):
    """Base class for all Infoblox related objects

    _fields - fields that represents NIOS object (WAPI fields) and
        are sent to NIOS on object creation
    _search_fields - fields that can be used to find object on NIOS side
    _shadow_fields - fields that object usually has but they should not
        be sent to NIOS. These fields can be received from NIOS. Examples:
        [_ref, is_default]
    _return_fields - fields requested to be returned from NIOS side
         if object is found/created
    _infoblox_type - string representing wapi type of described object
    _remap - dict that maps user faced names into internal
         representation (_fields)
    _custom_field_processing - dict that define rules (lambda) for building
         objects from data returned by NIOS side
    _ip_version - ip version of the object, used to mark version
        specific classes. Value other than None indicates that
        no versioned class lookup needed.
    """
    _fields = []
    _search_fields = []
    _shadow_fields = []
    _infoblox_type = None
    _remap = {}

    _return_fields = []
    _custom_field_processing = {}
    _ip_version = None

    def __new__(cls, connector, **kwargs):
        return super(InfobloxObject,
                     cls).__new__(cls.get_class_from_args(kwargs))

    def __init__(self, connector, **kwargs):
        self.connector = connector
        super(InfobloxObject, self).__init__(**kwargs)

    def update_from_dict(self, ip_dict):
        mapped_args = self._remap_fields(ip_dict)
        for field in self._fields + self._shadow_fields:
            if field in ip_dict:
                setattr(self, field, mapped_args[field])

    def __eq__(self, other):
        if isinstance(other, self.__class__):
            for field in self._fields:
                if getattr(self, field) != getattr(other, field):
                    return False
            return True
        return False

    @classmethod
    def from_dict(cls, connector, ip_dict):
        mapping = cls._custom_field_processing
        # Process fields that require building themselves as objects
        for field in mapping:
            if field in ip_dict:
                ip_dict[field] = mapping[field](ip_dict[field])
        return cls(connector, **ip_dict)

    @staticmethod
    def value_to_dict(value):
        return value.to_dict() if hasattr(value, 'to_dict') else value

    def field_to_dict(self, field):
        """Read field value and converts to dict if possible"""
        value = getattr(self, field)
        if isinstance(value, (list, tuple)):
            return [self.value_to_dict(val) for val in value]
        return self.value_to_dict(value)

    def to_dict(self, search_fields=None):
        """Builds dict without None object fields"""
        fields = self._fields
        if search_fields == 'only':
            fields = self._search_fields
        elif search_fields == 'exclude':
            # exclude search fields for update actions
            fields = [field for field in self._fields
                      if field not in self._search_fields]

        return {field: self.field_to_dict(field) for field in fields
                if getattr(self, field, None) is not None}

    @staticmethod
    def _object_from_reply(parse_class, connector, reply):
        if not reply:
            return None
        if isinstance(reply, dict):
            return parse_class.from_dict(connector, reply)

        # If no return fields were requested reply contains only string
        # with reference to object
        return_dict = {'_ref': reply}
        return parse_class.from_dict(connector, return_dict)

    @classmethod
    def create(cls, connector, check_if_exists=True,
               update_if_exists=False, **kwargs):
        local_obj = cls(connector, **kwargs)
        if check_if_exists:
            if local_obj.fetch():
                LOG.info(("Infoblox %(obj_type)s already exists: "
                          "%(ib_obj)s"),
                         {'obj_type': local_obj.infoblox_type,
                          'ib_obj': local_obj})
                if not update_if_exists:
                    return local_obj
        reply = None
        if not local_obj.ref:
            reply = connector.create_object(local_obj.infoblox_type,
                                            local_obj.to_dict(),
                                            local_obj.return_fields)
            LOG.info("Infoblox %(obj_type)s was created: %(ib_obj)s",
                     {'obj_type': local_obj.infoblox_type,
                      'ib_obj': local_obj})
        elif update_if_exists:
            update_fields = local_obj.to_dict(search_fields='exclude')
            reply = connector.update_object(local_obj.ref,
                                            update_fields,
                                            local_obj.return_fields)
            LOG.info('Infoblox object was updated: %s', local_obj.ref)
        return cls._object_from_reply(local_obj, connector, reply)

    @classmethod
    def _search(cls, connector, return_fields=None,
                search_extattrs=None, force_proxy=False, **kwargs):
        ib_obj_for_search = cls(connector, **kwargs)
        search_dict = ib_obj_for_search.to_dict(search_fields='only')
        if not return_fields and ib_obj_for_search.return_fields:
            return_fields = ib_obj_for_search.return_fields
        reply = connector.get_object(ib_obj_for_search.infoblox_type,
                                     search_dict,
                                     return_fields=return_fields,
                                     extattrs=search_extattrs,
                                     force_proxy=force_proxy)
        return reply, ib_obj_for_search

    @classmethod
    def search(cls, connector, **kwargs):
        ib_obj, parse_class = cls._search(
            connector, **kwargs)
        if ib_obj:
            return parse_class.from_dict(connector, ib_obj[0])

    @classmethod
    def search_all(cls, connector,  **kwargs):
        ib_objects, parsing_class = cls._search(
            connector, **kwargs)
        if ib_objects:
            return [parsing_class.from_dict(connector, obj)
                    for obj in ib_objects]
        return []

    def fetch(self):
        """Fetch object from NIOS by _ref or searchfields

        Update existent object with fields returned from NIOS
        Return True on successful object fetch
        """
        if self.ref:
            reply = self.connector.get_object(
                self.ref, return_fields=self.return_fields)
            if reply:
                self.update_from_dict(reply)
                return True

        search_dict = self.to_dict(search_fields='only')
        reply = self.connector.get_object(self.infoblox_type,
                                          search_dict,
                                          return_fields=self.return_fields)
        if reply:
            self.update_from_dict(reply[0])
            return True
        return False

    def update(self):
        update_fields = self.to_dict(search_fields='exclude')
        ib_obj = self.connector.update_object(self.ref,
                                              update_fields,
                                              self.return_fields)
        LOG.info('Infoblox object was updated: %s', self.ref)
        return self._object_from_reply(self, self.connector, ib_obj)

    def delete(self):
        try:
            self.connector.delete_object(self.ref)
        except ib_ex.InfobloxCannotDeleteObject as e:
            LOG.info("Failed to delete an object: %s", e)

    @property
    def infoblox_type(self):
        return self._infoblox_type

    @property
    def return_fields(self):
        return self._return_fields

    @classmethod
    def get_class_from_args(cls, kwargs):
        # skip processing if cls already versioned class
        if cls._ip_version:
            return cls

        for field in ['ip', 'cidr', 'start_ip', 'ip_address']:
            if field in kwargs:
                if ib_utils.determine_ip_version(kwargs[field]) == 6:
                    return cls.get_v6_class()
                else:
                    return cls.get_v4_class()
        # fallback to IPv4 object if find nothing
        return cls.get_v4_class()

    @classmethod
    def get_v4_class(cls):
        return cls

    @classmethod
    def get_v6_class(cls):
        return cls


class Network(InfobloxObject):
    _fields = ['network_view', 'network', 'template',
               'options', 'nameservers', 'members', 'gateway_ip',
               'extattrs']
    _search_fields = ['network_view', 'network']
    _shadow_fields = ['_ref']
    _return_fields = ['network_view', 'network', 'options', 'members']
    _remap = {'cidr': 'network'}

    @classmethod
    def get_v4_class(cls):
        return NetworkV4

    @classmethod
    def get_v6_class(cls):
        return NetworkV6

    @staticmethod
    def _build_member(members):
        if not members:
            return None
        return [AnyMember.from_dict(m) for m in members]

    _custom_field_processing = {'members': _build_member.__func__}


class NetworkV4(Network):
    _infoblox_type = 'network'
    _ip_version = 4


class NetworkV6(Network):
    _infoblox_type = 'ipv6network'
    _ip_version = 6


class HostRecord(InfobloxObject):
    """Base class for HostRecords

    HostRecord uses ipvXaddr for search and ipvXaddrs for object creation.
    ipvXaddr and ipvXaddrs are quite different:
    ipvXaddr is single ip as a string
    ipvXaddrs is list of dicts with ipvXaddr, mac, configure_for_dhcp
    and host keys.
    In 'ipvXaddr' 'X' stands for 4 or 6 depending on ip version of the class.

    To find HostRecord use next syntax:
    hr = HostRecord.search(connector, ip='192.168.1.25', view='some-view')

    To create host record create IP object first:
    ip = IP(ip='192.168.1.25', mac='aa:ab;ce:12:23:34')
    hr = HostRecord.create(connector, ip=ip, view='some-view')

    """
    _infoblox_type = 'record:host'

    @classmethod
    def get_v4_class(cls):
        return HostRecordV4

    @classmethod
    def get_v6_class(cls):
        return HostRecordV6

    def _ip_setter(self, ipaddr_name, ipaddrs_name, ips):
        """Setter for ip fields

        Accept as input string or list of IP instances.
        String case:
            only ipvXaddr is going to be filled, that is enough to perform
            host record search using ip
        List of IP instances case:
            ipvXaddrs is going to be filled with ips content,
            so create can be issues, since fully prepared IP objects in place.
            ipXaddr is also filled to be able perform search on NIOS
            and verify that no such host record exists yet.
        """
        if isinstance(ips, six.string_types):
            setattr(self, ipaddr_name, ips)
        elif isinstance(ips, (list, tuple)) and isinstance(ips[0], IP):
            setattr(self, ipaddr_name, ips[0].ip)
            setattr(self, ipaddrs_name, ips)
        elif isinstance(ips, IP):
            setattr(self, ipaddr_name, ips.ip)
            setattr(self, ipaddrs_name, [ips])
        elif ips is None:
            setattr(self, ipaddr_name, None)
            setattr(self, ipaddrs_name, None)
        else:
            raise ValueError(
                "Invalid format of ip passed in: %s."
                "Should be string or list of NIOS IP objects." % ips)


class HostRecordV4(HostRecord):
    """HostRecord for IPv4"""
    _fields = ['ipv4addrs', 'view', 'extattrs', 'name']
    _search_fields = ['view', 'ipv4addr']
    _shadow_fields = ['_ref', 'ipv4addr']
    _return_fields = ['ipv4addrs']
    _remap = {'ip': 'ipv4addrs'}
    _ip_version = 4

    @property
    def ipv4addrs(self):
        return self._ipv4addrs

    @ipv4addrs.setter
    def ipv4addrs(self, ips):
        """Setter for ipv4addrs/ipv4addr"""
        self._ip_setter('ipv4addr', '_ipv4addrs', ips)

    @staticmethod
    def _build_ipv4(ips_v4):
        if not ips_v4:
            raise ib_ex.HostRecordNotPresent()
        ip = ips_v4[0]['ipv4addr']
        if not ib_utils.is_valid_ip(ip):
            raise ib_ex.InfobloxInvalidIp(ip=ip)
        return [IPv4.from_dict(ip_addr) for ip_addr in ips_v4]

    _custom_field_processing = {'ipv4addrs': _build_ipv4.__func__}


class HostRecordV6(HostRecord):
    """HostRecord for IPv6"""
    _fields = ['ipv6addrs', 'view', 'extattrs',  'name']
    _search_fields = ['ipv6addr', 'view', 'name']
    _shadow_fields = ['_ref', 'ipv6addr']
    _return_fields = ['ipv6addrs']
    _remap = {'ip': 'ipv6addrs'}
    _ip_version = 6

    @property
    def ipv6addrs(self):
        return self._ipv6addrs

    @ipv6addrs.setter
    def ipv6addrs(self, ips):
        """Setter for ipv6addrs/ipv6addr"""
        self._ip_setter('ipv6addr', '_ipv6addrs', ips)

    @staticmethod
    def _build_ipv6(ips_v6):
        if not ips_v6:
            raise ib_ex.HostRecordNotPresent()
        ip = ips_v6[0]['ipv6addr']
        if not ib_utils.is_valid_ip(ip):
            raise ib_ex.InfobloxInvalidIp(ip=ip)
        return [IPv6.from_dict(ip_addr) for ip_addr in ips_v6]

    _custom_field_processing = {'ipv6addrs': _build_ipv6.__func__}


class SubObjects(BaseObject):
    """Base class for objects that do not require all InfobloxObject power"""

    @classmethod
    def from_dict(cls, ip_dict):
        return cls(**ip_dict)

    def to_dict(self):
        return {field: getattr(self, field) for field in self._fields
                if getattr(self, field, None) is not None}


class IP(SubObjects):
    _fields = []
    _shadow_fields = ['_ref', 'ip']
    _remap = {}
    ip_version = None

    # better way for mac processing?
    @classmethod
    def create(cls, ip=None, mac=None, **kwargs):
        if ip is None:
            raise ValueError
        if ib_utils.determine_ip_version(ip) == 6:
            return IPv6(ip=ip, duid=ib_utils.generate_duid(mac),
                        **kwargs)
        else:
            return IPv4(ip=ip, mac=mac, **kwargs)

    def __eq__(self, other):
        if isinstance(other, six.string_types):
            return self.ip == other
        elif isinstance(other, self.__class__):
            return self.ip == other.ip
        return False

    @property
    def zone_auth(self):
        if self.host is not None:
            return self.host.partition('.')[2]

    @property
    def hostname(self):
        if self.host is not None:
            return self.host.partition('.')[0]

    @property
    def ip(self):
        # Convert IPAllocation objects to string
        if hasattr(self, '_ip'):
            return str(self._ip)

    @ip.setter
    def ip(self, ip):
        self._ip = ip


class IPv4(IP):
    _fields = ['ipv4addr', 'configure_for_dhcp', 'host', 'mac']
    _remap = {'ipv4addr': 'ip'}
    ip_version = 4


class IPv6(IP):
    _fields = ['ipv6addr', 'configure_for_dhcp', 'host', 'duid']
    _remap = {'ipv6addr': 'ip'}
    ip_version = 6


class AnyMember(SubObjects):
    _fields = ['_struct', 'name', 'ipv4addr', 'ipv6addr']
    _shadow_fields = ['ip']

    @property
    def ip(self):
        if hasattr(self, '_ip'):
            return str(self._ip)

    @ip.setter
    def ip(self, ip):
        # AnyMember represents both ipv4 and ipv6 objects, so don't need
        # versioned object for that. Just set v4 or v6 field additionally
        # to setting shadow 'ip' field itself.
        # So once dict is generated by to_dict only versioned ip field
        # to be shown.
        self._ip = ip
        if ib_utils.determine_ip_version(ip) == 6:
            self.ipv6addr = ip
        else:
            self.ipv4addr = ip


class IPRange(InfobloxObject):
    _fields = ['start_addr', 'end_addr', 'network_view',
               'network', 'extattrs', 'disable']
    _remap = {'cidr': 'network'}
    _search_fields = ['network_view', 'start_addr']
    _shadow_fields = ['_ref']
    _return_fields = ['start_addr', 'end_addr', 'network_view', 'extattrs']

    @classmethod
    def get_v4_class(cls):
        return IPRangeV4

    @classmethod
    def get_v6_class(cls):
        return IPRangeV6


class IPRangeV4(IPRange):
    _infoblox_type = 'range'
    _ip_version = 4


class IPRangeV6(IPRange):
    _infoblox_type = 'ipv6range'
    _ip_version = 6


class FixedAddress(InfobloxObject):
    @classmethod
    def get_v4_class(cls):
        return FixedAddressV4

    @classmethod
    def get_v6_class(cls):
        return FixedAddressV6

    @property
    def ip(self):
        if hasattr(self, '_ip') and self._ip:
            return str(self._ip)

    @ip.setter
    def ip(self, ip):
        self._ip = ip


class FixedAddressV4(FixedAddress):
    _infoblox_type = 'fixedaddress'
    _fields = ['ipv4addr', 'mac', 'network_view', 'extattrs']
    _search_fields = ['ipv4addr', 'mac', 'network_view']
    _shadow_fields = ['_ref', 'ip']
    _return_fields = ['ipv4addr', 'mac', 'network_view', 'extattrs']
    _remap = {'ipv4addr': 'ip'}
    _ip_version = 4


class FixedAddressV6(FixedAddress):
    """FixedAddress for IPv6"""
    _infoblox_type = 'ipv6fixedaddress'
    _fields = ['ipv6addr', 'duid', 'network_view', 'extattrs']
    _search_fields = ['ipv6addr', 'duid', 'network_view']
    _return_fields = ['ipv6addr', 'duid', 'network_view', 'extattrs']
    _shadow_fields = ['_ref', 'mac', 'ip']
    _remap = {'ipv6addr': 'ip'}
    _ip_version = 6

    @property
    def mac(self):
        return self._mac

    @mac.setter
    def mac(self, mac):
        """Set mac and duid fields

        To have common interface with FixedAddress accept mac address
        and set duid as a side effect.
        'mac' was added to _shadow_fields to prevent sending it out over wapi.
        """
        self._mac = mac
        if mac:
            self.duid = ib_utils.generate_duid(mac)
        elif not hasattr(self, 'duid'):
            self.duid = None


class ARecordBase(InfobloxObject):

    @classmethod
    def get_v4_class(cls):
        return ARecord

    @classmethod
    def get_v6_class(cls):
        return AAAARecord


class ARecord(ARecordBase):
    _infoblox_type = 'record:a'
    _fields = ['ipv4addr', 'name', 'view', 'extattrs']
    _search_fields = ['ipv4addr', 'view']
    _shadow_fields = ['_ref']
    _remap = {'ip': 'ipv4addr'}
    _ip_version = 4


class AAAARecord(ARecordBase):
    _infoblox_type = 'record:aaaa'
    _fields = ['ipv6addr', 'name', 'view', 'extattrs']
    _search_fields = ['ipv6addr', 'view']
    _shadow_fields = ['_ref']
    _remap = {'ip': 'ipv6addr'}
    _ip_version = 6


class PtrRecord(InfobloxObject):
    _infoblox_type = 'record:ptr'

    @classmethod
    def get_v4_class(cls):
        return PtrRecordV4

    @classmethod
    def get_v6_class(cls):
        return PtrRecordV6


class PtrRecordV4(PtrRecord):
    _fields = ['view', 'ipv4addr', 'ptrdname', 'extattrs']
    _search_fields = ['view', 'ipv4addr']
    _shadow_fields = ['_ref']
    _remap = {'ip': 'ipv4addr'}
    _ip_version = 4


class PtrRecordV6(PtrRecord):
    _fields = ['view', 'ipv6addr', 'ptrdname', 'extattrs']
    _search_fields = ['view', 'ipv6addr']
    _shadow_fields = ['_ref']
    _remap = {'ip': 'ipv6addr'}
    _ip_version = 6


class NetworkView(InfobloxObject):
    _infoblox_type = 'networkview'
    _fields = ['name', 'extattrs']
    _search_fields = ['name']
    _shadow_fields = ['_ref', 'is_default']
    _ip_version = 'any'


class DNSView(InfobloxObject):
    _infoblox_type = 'view'
    _fields = ['name', 'network_view']
    _search_fields = ['name', 'network_view']
    _shadow_fields = ['_ref', 'is_default']
    _ip_version = 'any'


class DNSZone(InfobloxObject):
    _infoblox_type = 'zone_auth'
    _fields = ['_ref', 'fqdn', 'view', 'extattrs', 'zone_format', 'ns_group',
               'prefix', 'grid_primary', 'grid_secondaries']
    _search_fields = ['fqdn', 'view']
    _shadow_fields = ['_ref']
    _ip_version = 'any'

    @staticmethod
    def _build_member(members):
        if not members:
            return None
        return [AnyMember.from_dict(m) for m in members]

    _custom_field_processing = {
        'primary_dns_members': _build_member.__func__,
        'secondary_dns_members': _build_member.__func__}


class Member(InfobloxObject):
    _infoblox_type = 'member'
    _fields = ['host_name', 'ipv6_setting', 'node_info', 'vip_setting']
    _search_fields = ['host_name']
    _shadow_fields = ['_ref', 'ip']
    _ip_version = 'any'
    _remap = {'name': 'host_name'}


class IPAddress(InfobloxObject):
    _fields = ['network_view', 'ip_address', 'objects']
    _search_fields = ['network_view', 'ip_address']
    _shadow_fields = ['_ref']
    _return_fields = ['objects']

    @classmethod
    def get_v4_class(cls):
        return IPv4Address

    @classmethod
    def get_v6_class(cls):
        return IPv6Address


class IPv4Address(IPAddress):
    _infoblox_type = 'ipv4address'
    _ip_version = 4


class IPv6Address(IPAddress):
    _infoblox_type = 'ipv6address'
    _ip_version = 6


class IPAllocation(object):

    def __init__(self, address, next_available_ip):
        self.ip_version = ib_utils.determine_ip_version(address)
        self.next_available_ip = next_available_ip

    def __repr__(self):
        return "IPAllocation: {0}".format(self.next_available_ip)

    def __str__(self):
        return str(self.next_available_ip)

    @classmethod
    def next_available_ip_from_cidr(cls, net_view_name, cidr):
        return cls(cidr, 'func:nextavailableip:'
                         '{cidr:s},{net_view_name:s}'.format(**locals()))

    @classmethod
    def next_available_ip_from_range(cls, net_view_name, first_ip, last_ip):
        return cls(first_ip, 'func:nextavailableip:{first_ip}-{last_ip},'
                             '{net_view_name}'.format(**locals()))
