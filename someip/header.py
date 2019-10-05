
import asyncio
import dataclasses
import enum
import ipaddress
import struct
import socket
import typing


T = typing.TypeVar('T')


class ParseError(RuntimeError):
    pass


class IncompleteReadError(ParseError):
    pass


class DefaultEnum(enum.Enum):
    '''
    when an undefined value is retrieved, a pseudo member is created to represent that value.
    assign a % format string to __default_name__ to get a string representation of undefined values.

    if assigning a value to multiple names, the first name becomes the representation of the value,
    while the other names become aliases to the first name.
    if assigning to multiple names in a single statement, the leftmost name is considered the first
    name.

    requires python3.6+.

    example:

    class Foo(int, DefaultEnum):
        __default_name__ = 'RESERVED_0x%02x'
        VALUE_A = ALIAS_A = 0
        VALUE_B = 1

    >>> Foo(0)
    <Foo.VALUE_A: 0>

    >>> Foo(1)
    <Foo.VALUE_B: 1>

    >>> Foo(2)
    <Foo.RESERVED_0x02: 2>

    >>> Foo.ALIAS_A
    <Foo.VALUE_A: 0>
    '''

    __default_name__: typing.Optional[str] = None
    @classmethod
    def _missing_(cls, value):
        if not isinstance(value, int):
            raise ValueError('%r is not a valid %s' % (value, cls.__name__))
        new_member = cls._create_pseudo_member_(value)
        return new_member

    @classmethod
    def _create_pseudo_member_(cls, value):
        # construct singleton pseudo-members
        pseudo_member = int.__new__(cls, value)
        # pylint: disable=protected-access,attribute-defined-outside-init
        pseudo_member._name_ = cls.__default_name__
        if pseudo_member._name_:
            pseudo_member._name_ %= value
        pseudo_member._value_ = value
        # pylint: enable=protected-access,attribute-defined-outside-init
        # use setdefault in case another thread already created a composite
        # with this value
        return cls._value2member_map_.setdefault(value, pseudo_member)


class SOMEIPMessageType(DefaultEnum):
    __default_name__ = 'UNKNOWN_0x%02x'
    REQUEST = 0
    REQUEST_NO_RETURN = 1
    NOTIFICATION = 2
    REQUEST_ACK = 0x40
    REQUEST_NO_RETURN_ACK = 0x41
    NOTIFICATION_ACK = 0x42
    RESPONSE = 0x80
    ERROR = 0x81
    RESPONSE_ACK = 0xc0
    ERROR_ACK = 0xc1


class SOMEIPReturnCode(DefaultEnum):
    __default_name__ = 'UNKNOWN_0x%02x'
    E_OK = 0
    E_NOT_OK = 1
    E_UNKNOWN_SERVICE = 2
    E_UNKNOWN_METHOD = 3
    E_NOT_READY = 4
    E_NOT_REACHABLE = 5
    E_TIMEOUT = 6
    E_WRONG_PROTOCOL_VERSION = 7
    E_WRONG_INTERFACE_VERSION = 8
    E_MALFORMED_MESSAGE = 9
    E_WRONG_MESSAGE_TYPE = 10


def unpack(fmt, buf):
    if len(buf) < fmt.size:
        raise IncompleteReadError(f'can not parse {fmt.format!r}, got only {len(buf)} bytes')
    return fmt.unpack(buf[:fmt.size]), buf[fmt.size:]


@dataclasses.dataclass
class SOMEIPHeader:
    __format: typing.ClassVar[struct.Struct] = struct.Struct('!HHIHHBBBB')
    service_id: int
    method_id: int
    client_id: int
    session_id: int
    protocol_version: int
    interface_version: int
    message_type: SOMEIPMessageType
    return_code: SOMEIPReturnCode
    payload: bytes = dataclasses.field(default=b'')

    @classmethod
    def parse(cls, buf: bytes) -> typing.Tuple['SOMEIPHeader', bytes]:
        '''
        parses SOMEIP packet in buffer, returns tuple (S, B)
        where S is parsed SOMEIPHeader including payload
        and B is unparsed rest of buffer
        '''
        (sid, mid, size, cid, sessid, pv, iv, mt_b, rc_b), buf_rest = unpack(cls.__format, buf)
        if pv != 1:
            raise ParseError(f'bad someip protocol version 0x{pv:02x}, expected 0x01')
        if len(buf_rest) < size - 8:
            raise IncompleteReadError(f'packet too short, expected {size+4}, got {len(buf)}')
        payload_b, buf_rest = buf_rest[:size-8], buf_rest[size-8:]

        mt = SOMEIPMessageType(mt_b)
        rc = SOMEIPReturnCode(rc_b)

        parsed = cls(service_id=sid, method_id=mid, client_id=cid, session_id=sessid,
                     protocol_version=pv, interface_version=iv, message_type=mt, return_code=rc,
                     payload=payload_b)

        return parsed, buf_rest

    @classmethod
    async def read(cls, buf: asyncio.StreamReader) -> 'SOMEIPHeader':
        hdr_b = await buf.readexactly(cls.__format.size)
        sid, mid, size, cid, sessid, pv, iv, mt_b, rc_b = cls.__format.unpack(hdr_b)
        if pv != 1:
            raise ParseError(f'bad someip protocol version 0x{pv:02x}, expected 0x01')

        payload_b = await buf.readexactly(size-8)

        mt = SOMEIPMessageType(mt_b)
        rc = SOMEIPReturnCode(rc_b)

        parsed = cls(service_id=sid, method_id=mid, client_id=cid, session_id=sessid,
                     protocol_version=pv, interface_version=iv, message_type=mt, return_code=rc,
                     payload=payload_b)

        return parsed

    def build(self) -> bytes:
        size = len(self.payload) + 8
        hdr = self.__format.pack(self.service_id, self.method_id, size, self.client_id,
                                self.session_id, self.protocol_version, self.interface_version,
                                self.message_type.value, self.return_code.value)
        return hdr + self.payload


class SOMEIPReader:
    def __init__(self, reader: asyncio.StreamReader):
        self.reader = reader

    async def read(self) -> typing.Optional[SOMEIPHeader]:
        return await SOMEIPHeader.read(self.reader)

    def at_eof(self):
        return self.reader.at_eof()


class SOMEIPSDEntryType(DefaultEnum):
    __default_name__ = 'UNKNOWN_0x%02x'
    FindService = 0
    OfferService = 1
    Subscribe = 6
    SubscribeAck = 7


@dataclasses.dataclass
class SOMEIPSDEntry:
    __format: typing.ClassVar[struct.Struct] = struct.Struct('!BBBBHHBBHI')
    sd_type: SOMEIPSDEntryType
    option_index_1: int
    option_index_2: int
    num_options_1: int
    num_options_2: int
    service_id: int
    instance_id: int
    major_version: int
    ttl: int
    minver_or_counter: int

    @property
    def service_minor_version(self) -> int:
        if self.sd_type not in (SOMEIPSDEntryType.FindService, SOMEIPSDEntryType.OfferService):
            raise TypeError(f'SD entry is type {self.sd_type}, does not have service_minor_version')
        return self.minver_or_counter

    @property
    def eventgroup_counter(self) -> int:
        if self.sd_type not in (SOMEIPSDEntryType.Subscribe, SOMEIPSDEntryType.SubscribeAck):
            raise TypeError(f'SD entry is type {self.sd_type}, does not have eventgroup_counter')
        return self.minver_or_counter

    @classmethod
    def parse(cls, buf: bytes) -> typing.Tuple['SOMEIPSDEntry', bytes]:
        (sd_type_b, oi1, oi2, numopt, sid, iid, majv, ttl_hi, ttl_lo, val), buf_rest \
                = unpack(cls.__format, buf)
        sd_type = SOMEIPSDEntryType(sd_type_b)
        no1 = numopt >> 4
        no2 = numopt & 0x0f
        ttl = (ttl_hi << 16) | ttl_lo

        if sd_type in (SOMEIPSDEntryType.Subscribe, SOMEIPSDEntryType.SubscribeAck):
            if val & 0xffffff00:
                raise ParseError('expected eventgroup counter to be 8-bit with 24 upper bits zeros')

        parsed = cls(sd_type=sd_type, option_index_1=oi1, option_index_2=oi2,
                     num_options_1=no1, num_options_2=no2, service_id=sid, instance_id=iid,
                     major_version=majv, ttl=ttl, minver_or_counter=val)

        return parsed, buf_rest

    def build(self) -> bytes:
        return self.__format.pack(self.sd_type.value, self.option_index_1, self.option_index_2,
                                 (self.num_options_1 << 4) | self.num_options_2, self.service_id,
                                 self.instance_id, self.major_version,
                                 self.ttl >> 16, self.ttl & 0xffff,
                                 self.minver_or_counter)


class SOMEIPSDOption:
    __format: typing.ClassVar[struct.Struct] = struct.Struct('!HB')
    options: typing.Dict[int, typing.Type['SOMEIPSDAbstractOption']] = {}

    @classmethod
    def register(cls, option_cls: typing.Type['SOMEIPSDAbstractOption']) \
            -> typing.Type['SOMEIPSDAbstractOption']:
        cls.options[option_cls.type_] = option_cls
        return option_cls

    @classmethod
    def parse(cls, buf: bytes) -> typing.Tuple['SOMEIPSDOption', bytes]:
        (len_b, type_b), buf_rest = unpack(cls.__format, buf)
        opt_b, buf_rest = buf_rest[:len_b], buf_rest[len_b:]

        opt_cls = cls.options.get(type_b)
        if not opt_cls:
            return SOMEIPSDUnknownOption(type_=type_b, payload=opt_b), buf_rest

        return opt_cls.parse_option(opt_b), buf_rest

    def build_option(self, type_b: int, buf: bytes) -> bytes:
        return self.__format.pack(len(buf), type_b) + buf

    def build(self) -> bytes: ...


@dataclasses.dataclass
class SOMEIPSDUnknownOption(SOMEIPSDOption):
    type_: int
    payload: bytes

    def build(self) -> bytes:
        return self.build_option(self.type_, self.payload)


class SOMEIPSDAbstractOption(SOMEIPSDOption):
    type_: typing.ClassVar[int]

    @classmethod
    def parse_option(cls, buf: bytes) -> 'SOMEIPSDAbstractOption': ...


@SOMEIPSDOption.register
@dataclasses.dataclass
class SOMEIPSDLoadBalancingOption(SOMEIPSDAbstractOption):
    type_: typing.ClassVar[int] = 2
    priority: int
    weight: int

    @classmethod
    def parse_option(cls, buf: bytes) -> 'SOMEIPSDLoadBalancingOption':
        if len(buf) != 5:
            raise ParseError(f'SD load balancing option with wrong payload length {len(buf)} != 5')
        if buf[0] != 0:
            raise ParseError(f'SD load balancing option with reserved = 0x{buf[0]:02x} != 0')

        prio, weight = struct.unpack('!HH', buf[1:])
        return cls(priority=prio, weight=weight)

    def build(self) -> bytes:
        return self.build_option(self.type_, struct.pack('!BHH', 0, self.priority, self.weight))


@SOMEIPSDOption.register
@dataclasses.dataclass
class SOMEIPSDConfigOption(SOMEIPSDAbstractOption):
    type_: typing.ClassVar[int] = 1
    configs: typing.Sequence[typing.Tuple[str, typing.Optional[str]]]

    @classmethod
    def parse_option(cls, buf: bytes) -> 'SOMEIPSDConfigOption':
        if len(buf) < 2:
            raise ParseError(f'SD config option with wrong payload length {len(buf)} < 2')
        if buf[0] != 0:
            raise ParseError(f'SD config option with reserved = 0x{buf[0]:02x} != 0')

        b = buf[1:]
        nextlen, b = b[0], b[1:]

        configs: typing.List[typing.Tuple[str, typing.Optional[str]]] = []

        while nextlen != 0:
            if len(b) < nextlen + 1:
                raise ParseError(f'SD config option length {nextlen} too big for remaining'
                                 f' option buffer {b!r}')

            cfg_str, b = b[:nextlen], b[nextlen:]

            split = cfg_str.find(b'=')
            if split == -1:
                configs.append((cfg_str.decode('ascii'), None))
            else:
                key, value = cfg_str[:split], cfg_str[split+1:]
                configs.append((key.decode('ascii'), value.decode('ascii')))
            nextlen, b = b[0], b[1:]
        return cls(configs=configs)

    def build(self) -> bytes:
        buf = bytearray([0])
        for k, v in self.configs:
            if v is not None:
                buf.append(len(k) + len(v) + 1)
                buf += k.encode('ascii')
                buf += b'='
                buf += v.encode('ascii')
            else:
                buf.append(len(k))
                buf += k.encode('ascii')
        buf.append(0)
        return self.build_option(self.type_, buf)


class L4Protocols(DefaultEnum):
    __default_name__ = 'UNKNOWN_0x%02x'
    TCP = socket.IPPROTO_TCP
    UDP = socket.IPPROTO_UDP


@SOMEIPSDOption.register
@dataclasses.dataclass
class SOMEIPSDIPv4EndpointOption(SOMEIPSDAbstractOption):
    __format: typing.ClassVar[struct.Struct] = struct.Struct('!B4sBBH')
    type_: typing.ClassVar[int] = 4
    address: ipaddress.IPv4Address
    l4proto: L4Protocols
    port: int

    @classmethod
    def parse_option(cls, buf: bytes) -> 'SOMEIPSDIPv4EndpointOption':
        if len(buf) != 9:
            raise ParseError(f'SD IPv4 option with wrong payload length {len(buf)} != 9')

        r1, addr_b, r2, l4proto_b, port = cls.__format.unpack(buf)

        if r1 != 0:
            raise ParseError(f'SD IPv4 option with reserved 1 = 0x{r1:02x} != 0')
        if r2 != 0:
            raise ParseError(f'SD IPv4 option with reserved 2 = 0x{r2:02x} != 0')

        addr = ipaddress.IPv4Address(addr_b)
        l4proto = L4Protocols(l4proto_b)

        return cls(address=addr, l4proto=l4proto, port=port)

    def build(self) -> bytes:
        payload = self.__format.pack(0, self.address.packed, 0, self.l4proto.value, self.port)
        return self.build_option(self.type_, payload)


@dataclasses.dataclass
class SOMEIPSDHeader:
    flag_reboot: bool
    flag_unicast: bool
    flags_unknown: int
    entries: typing.Sequence[SOMEIPSDEntry]
    options: typing.Sequence[SOMEIPSDOption]

    @classmethod
    def parse(cls, buf: bytes) -> typing.Tuple['SOMEIPSDHeader', bytes]:
        if len(buf) < 12:
            raise IncompleteReadError(f'can not parse SOMEIPSDHeader, got only {len(buf)} bytes')

        if buf[1:4] != b'\0\0\0':
            raise ParseError(f'SD header with reserved = {buf[1:4]!r} != 0')

        flags = buf[0]

        entries_length = struct.unpack('!I', buf[4:8])[0]
        rest_buf = buf[8:]
        if len(rest_buf) < entries_length + 4:
            raise IncompleteReadError(f'can not parse SOMEIPSDHeader, entries length too big'
                                      f' ({entries_length})')
        entries_buffer, rest_buf = rest_buf[:entries_length], rest_buf[entries_length:]

        options_length = struct.unpack('!I', rest_buf[:4])[0]
        rest_buf = rest_buf[4:]
        if len(rest_buf) < options_length:
            raise IncompleteReadError(f'can not parse SOMEIPSDHeader, options length too big'
                                      f' ({options_length}')
        options_buffer, rest_buf = rest_buf[:options_length], rest_buf[options_length:]

        entries = []
        while entries_buffer:
            entry, entries_buffer = SOMEIPSDEntry.parse(entries_buffer)
            entries.append(entry)

        options = []
        while options_buffer:
            option, options_buffer = SOMEIPSDOption.parse(options_buffer)
            options.append(option)

        flag_reboot = bool(flags & 0x80)
        flags &= ~0x80

        flag_unicast = bool(flags & 0x40)
        flags &= ~0x40

        parsed = cls(flag_reboot=flag_reboot, flag_unicast=flag_unicast, flags_unknown=flags,
                     entries=entries, options=options)
        return parsed, rest_buf

    def build(self) -> bytes:
        flags = self.flags_unknown

        if self.flag_reboot:
            flags |= 0x80

        if self.flag_unicast:
            flags |= 0x40

        buf = bytearray([flags, 0, 0, 0])

        entries_buf = b''.join(e.build() for e in self.entries)
        options_buf = b''.join(e.build() for e in self.options)

        buf += struct.pack('!I', len(entries_buf))
        buf += entries_buf
        buf += struct.pack('!I', len(options_buf))
        buf += options_buf

        return buf
