"""ASN.1 types.

Spec: http://www.itu.int/ITU-T/studygroups/com17/languages/X.690-0207.pdf
See also http://luca.ntop.org/Teaching/Appunti/asn1.html for a good introduction
to ASN.1.

This module implements a restricted encoder/decoder for a subset of ASN.1 types.

The decoder has a strict and non-strict mode. Non-strict mode tolerates selected
non-fatal DER decoding errors. The encoder is DER-only.

Generic decoding is not supported: objects can only be decoded against a
predefined ASN.1 type. However, applications can derive arbitrary custom ASN.1
type specifications from the supported base types.

Constraints (e.g., on the length of an ASN.1 string value) are not supported,
and should be checked at application level, where necessary.
"""

import abc
import collections
import functools

from ct.crypto import error
from ct.crypto.my_asn1 import tag


_ZERO = "\x00"
_MINUS_ONE = "\xff"


def encode_int(value, signed=True):
    """Encode an integer.

    Args:
        value: an integral value.
        signed: if True, encode in two's complement form. If False, encode as
            an unsigned integer.

    Returns:
        a variable-length string representing the encoded integer.
    """
    if not signed and value < 0:
        raise ValueError("Unsigned integer cannot be negative")

    if not value:
        return _ZERO
    if value == -1:
        return _MINUS_ONE

    int_bytes = bytearray()

    while value != 0 and value != -1:
        int_bytes.append(value & 0xff)
        value >>= 8

    if signed:
        # In two's complement form, negative values have the most significant
        # bit set, thus we:
        if value == -1 and int_bytes[-1] <= 127:
            # Add a "-1"-byte for indicating a negative value.
            int_bytes.append(0xff)
        elif value == 0 and int_bytes[-1] > 127:
            # Add a "0"-byte for indicating a positive value.
            int_bytes.append(0)

    int_bytes.reverse()
    return str(int_bytes)


def decode_int(buf, signed=True):
    """Decode an integer.

    Args:
        buf: a string or string buffer.
        signed: if True, decode in two's complement form. If False, decode as
            an unsigned integer.

    Raises:
        ASN1Error.

    Returns:
        an integer.
    """
    if not buf:
        raise error.ASN1Error("Invalid integer encoding: empty value")

    leading = ord(buf[0])
    int_bytes = bytearray(buf[1:])

    if int_bytes:
      if leading == 0 and int_bytes[0] < 128:
        # 0x00 0x42 == 0x42
        raise error.ASN1Error("Extra leading 0-bytes in integer "
                              "encoding")
      elif signed and leading == 0xff and int_bytes[0] >= 128:
        # 0xff 0x82 == 0x82
        raise error.ASN1Error("Extra leading 0xff-bytes in negative "
                              "integer encoding")

    if signed and leading > 127:
            leading -= 256

    for b in int_bytes:
        leading <<= 8
        leading += b

    return leading


# Lengths between 0 and 127 are encoded as a single byte.
# Lengths greater than 127 are encoded as follows:
#   * MSB of first byte is 1 and remaining bits encode the number of
#     additional bytes.
#   * Remaining bytes encode the length.
_MULTIBYTE_LENGTH = 0x80
_MULTIBYTE_LENGTH_MASK = 0x7f


def encode_length(length):
    """Encode an integer.

    Args:
        length: a non-negative integral value.

    Returns:
        a string.
    """
    if length <= 127:
        return chr(length)
    encoded_length = encode_int(length, signed=False)
    return chr(_MULTIBYTE_LENGTH | len(encoded_length)) + encoded_length


def read_length(buf):
    """Read an ASN.1 object length from the beginning of the buffer.

    Args:
        buf: a string or string buffer.
        strict: if False, tolerate encoding with a non-minimal number of octets.

    Raises:
        ASN1Error.
    Returns:
        a (length, rest) tuple consisting of a non-negative integer representing
        the length of an ASN.1 object, and the remaining bytes.
    """
    if not buf:
        raise error.ASN1Error("Invalid length encoding: empty value")
    length, rest = ord(buf[0]), buf[1:]
    if length <= 127:
        return length, rest
    length &= _MULTIBYTE_LENGTH_MASK
    if len(rest) < length:
        raise error.ASN1Error("Invalid length encoding")
    return (decode_int(rest[:length], signed=False), rest[length:])


class Universal(object):
    """Apply a universal tag to the class.

    Can be used as a callable, or a decorator:

    Integer = Universal(2, tag.PRIMITIVE)(Abstract)

    is the same as

    @Universal(2, tag.PRIMITIVE)
    class Integer(Abstract):
        pass

    and defines a type with an ASN.1 integer tag.
    """

    def __init__(self, number, encoding):
        """Setup the tag.

        Args:
            number: the tag number.
            encoding: the encoding. One of tag.PRIMITIVE or tag.CONSTRUCTED.
        """
        self.tag = tag.Tag(number, tag.UNIVERSAL, encoding)

    def __call__(self, cls):
        """Apply the universal tag.

        Args:
            cls: class to modify. The class must have an empty 'tags'
                attribute.

        Returns:
            the class with a modified 'tags' attribute.

        Raises:
            TypeError: invalid application of the tag.
        """
        if cls.tags:
            raise TypeError("Cannot apply a UNIVERSAL tag to a tagged type.")
        cls.tags = (self.tag,)
        return cls


class Explicit(object):
    """Apply an explicit tag to the class.

    Can be used as a callable, or a decorator:

    MyInteger = Explicit(0, tag.APPLICATION)(Integer)

    is the same as

    @Explicit(0, tag.APPLICATION)
    class MyInteger(Integer):
        pass

    and results in a MyInteger type that is explicitly tagged with an
    application-class 0-tag.
    """

    def __init__(self, number, tag_class=tag.CONTEXT_SPECIFIC):
        """Setup the tag.

        Args:
            number: the tag number.
            tag_class: the tag class. One of tag.CONTEXT_SPECIFIC,
                tag.APPLICATION or tag.PRIVATE.

        Raises:
            TypeError: invalid application of the tag.
        """
        if tag_class == tag.UNIVERSAL:
            raise TypeError("Cannot tag with a UNIVERSAL tag")
        # Explicit tagging always results in constructed encoding.
        self._tag = tag.Tag(number, tag_class, tag.CONSTRUCTED)

    def __call__(self, cls):
        """Apply the explicit tag.

        Args:
            cls: class to modify. The class must have an iterable 'tags'
                attribute.
        Returns:
            the class with a modified 'tags' attribute.
        """
        tags = list(cls.tags)
        tags.append(self._tag)
        cls.tags = tuple(tags)
        return cls


class Implicit(object):
    """Apply an implicit tag to the class.

    Can be used as a callable, or a decorator:

    MyInteger = Implicit(0, tag.APPLICATION)(Integer)

    is the same as

    @Implicit(0, tag.APPLICATION)
    class MyInteger(Integer):
        pass

    and results in a MyInteger type whose tag is implicitly replaced with an
    application-class 0-tag.
    """

    def __init__(self, number, tag_class=tag.CONTEXT_SPECIFIC):
        """Setup the tag.

        Args:
            number: the tag number.
            tag_class: the tag class. One of tag.CONTEXT_SPECIFIC,
                tag.APPLICATION or tag.PRIVATE.

        Raises:
            TypeError: invalid application of the tag.
        """
        if tag_class == tag.UNIVERSAL:
            raise TypeError("Cannot tag with a UNIVERSAL tag")
        # We cannot precompute the tag because the encoding depends
        # on the existing tags.
        self._number = number
        self._tag_class = tag_class

    def __call__(self, cls):
        """Apply the implicit tag.

        Args:
            cls: class to modify. The class must have an iterable 'tags'
                attribute.

        Returns:
            the class with a modified 'tags' attribute.

        Raises:
            TypeError: invalid application of the tag.
        """
        if not cls.tags:
            raise TypeError("Cannot implicitly tag an untagged type")
        tags = list(cls.tags)
        # Only simple types and simple types derived via implicit tagging have a
        # primitive encoding, so the last tag determines the encoding type.
        tags[-1] = (tag.Tag(self._number, self._tag_class,
                            cls.tags[-1].encoding))
        cls.tags = tuple(tags)
        return cls


class Abstract(object):
    """Abstract base class."""
    __metaclass__ = abc.ABCMeta

    tags = ()

    @classmethod
    def explicit(cls, number, tag_class=tag.CONTEXT_SPECIFIC):
        """Dynamically create a new tagged type.

        Args:
            number: tag number.
            tag_class: tag class.

        Returns:
            a subtype of cls with the given explicit tag.
        """
        name = "%s.explicit(%d, %d)" % (cls.__name__, number, tag_class)

        # TODO(ekasper): the metaclass could register created types so we
        # return the _same_ type when called more than once with the same
        # arguments.
        mcs = cls.__metaclass__
        return_class = mcs(name, (cls,), {})
        return Explicit(number, tag_class)(return_class)

    @classmethod
    def implicit(cls, number, tag_class=tag.CONTEXT_SPECIFIC):
        """Dynamically create a new tagged type.

        Args:
            number: tag number.
            tag_class: tag class.

        Returns:
            a subtype of cls with the given implicit tag.
        """
        name = "%s.implicit(%d, %d)" % (cls.__name__, number, tag_class)
        mcs = cls.__metaclass__
        return_class = mcs(name, (cls,), {})
        return Implicit(number, tag_class)(return_class)

    def __init__(self, value=None, serialized_value=None, strict=True):
        """Initialize from a value or serialized buffer.

        Args:
            value: initializing value of an appropriate type. If the
                serialized_value is not set, the initializing value must be set.
            serialized_value: serialized inner value (with tags and lengths
                stripped).
            strict: if False, tolerate some non-fatal decoding errors.

        Raises:
            error.ASN1Error: decoding the serialized value failed.
            TypeError: invalid initializer.
        """
        if serialized_value is not None:
            self._value = self._decode_value(serialized_value, strict=strict)
        elif value is not None:
            self._value = self._convert_value(value)
        else:
            raise TypeError("Cannot initialize from None")

    @classmethod
    def _convert_value(cls, value):
        """Convert initializer to an appropriate value."""
        raise NotImplementedError

    @abc.abstractmethod
    def _decode_value(self, buf, strict=True):
        """Decode the initializer value from a buffer.

        Returns:
           the value of the object.
        """
        pass

    @property
    def value(self):
        """Get the value of the object.

        An ASN.1 object can always be reconstructed from its value.
        """
        # Usually either the immutable value, or a shallow copy of
        # the mutable value.
        raise NotImplementedError

    def __repr__(self):
        return "%s(%r)" % (self.__class__.__name__, self.value)

    def __str__(self):
        return str(self.value)

    @abc.abstractmethod
    def _encode_value(self):
        """Encode the contents, excluding length and tags.

        Returns:
            a string representing the encoded value.
        """
        pass

    # Implemented by Choice and Any.
    # Used when the type is untagged so that read() does not reveal a length.
    @classmethod
    def _read(cls, buf, strict=True):
        """Read the value from the beginning of a string or buffer."""
        raise NotImplementedError

    def encode(self):
        """Encode oneself.

        Returns:
            a string representing the encoded object.
        """
        encoded_value = self._encode_value()
        for t in self.tags:
            encoded_length = encode_length(len(encoded_value))
            encoded_value = t.value + encoded_length + encoded_value
        return encoded_value

    @classmethod
    def read(cls, buf, strict=True):
        """Read from a string or buffer.

        Args:
            buf: a string or string buffer.
            strict: if False, tolerate some non-fatal decoding errors.

        Returns:
            an tuple consisting of an instance of the class and the remaining
            bytes.
        """
        if cls.tags:
            for t in reversed(cls.tags):
                if buf[:len(t)] != t.value:
                    raise error.ASN1TagError(
                        "Invalid tag: expected %s, got %s while decoding %s" %
                        (t, buf[:len(t.value)], cls.__name__))
                # Logging statements are really expensive in the recursion even
                # if debug-level logging itself is disabled.
                # logging.debug("%s: read tag %s", cls.__name__, t)
                buf = buf[len(t):]
                decoded_length, buf = read_length(buf)
                # logging.debug("%s: read length %d", cls.__name__,
                #               decoded_length)
                if len(buf) < decoded_length:
                    raise error.ASN1Error("Invalid length encoding in %s: "
                                          "read length %d, remaining bytes %d" %
                                          (cls.__name__, decoded_length,
                                           len(buf)))
            value, rest = (cls(serialized_value=buf[:decoded_length],
                               strict=strict), buf[decoded_length:])

        else:
            # Untagged CHOICE and ANY; no outer tags to determine the length.
            value, rest = cls._read(buf, strict=strict)

        # logging.debug("%s: decoded value %s", cls.__name__, value)
        # logging.debug("Remaining bytes: %d", len(rest))
        return value, rest

    @classmethod
    def decode(cls, buf, strict=True):
        """Decode from a string or buffer.

        Args:
            buf: a string or string buffer.
            strict: if False, tolerate some non-fatal decoding errors.

        Returns:
            an instance of the class.
        """
        value, rest = cls.read(buf, strict=strict)
        if rest:
            raise error.ASN1Error("Invalid encoding: leftover bytes when "
                                  "decoding %s" % cls.__name__)
        return value

    # Compare by value.
    # Note this means objects with equal values do not necessarily have
    # equal encodings.
    def __eq__(self, other):
        return self.value == other

    def __ne__(self, other):
        return self.value != other


# Boilerplate code for some simple types whose value directly corresponds to a
# basic immutable type.
@functools.total_ordering
class Simple(Abstract):
    """Base class for Boolean, Integer, and string types."""

    def __hash__(self):
        return hash(self.value)

    def __lt__(self, other):
        return self.value < other

    def __bool__(self):
        return bool(self.value)

    def __int__(self):
        return int(self.value)

    def __nonzero__(self):
        return bool(self.value)


@Universal(1, tag.PRIMITIVE)
class Boolean(Simple):
    """Boolean."""
    _TRUE = "\xff"
    _FALSE = "\x00"

    @property
    def value(self):
        return self._value

    def _encode_value(self):
        return self._TRUE if self._value else self._FALSE

    @classmethod
    def _convert_value(cls, value):
        return bool(value)

    @classmethod
    def _decode_value(cls, buf, strict=True):
        if len(buf) != 1:
            raise error.ASN1Error("Invalid encoding")

        # Continuing here breaks re-encoding.
        if strict and buf[0] != cls._TRUE and buf[0] != cls._FALSE:
                raise error.ASN1Error("BER encoding of Boolean value: %s" %
                                      buf[0])
        value = False if buf[0] == cls._FALSE else True
        return value


@Universal(2, tag.PRIMITIVE)
class Integer(Simple):
    """Integer."""

    @property
    def value(self):
        return self._value

    def _encode_value(self):
        return encode_int(self._value)

    @classmethod
    def _convert_value(cls, value):
        return int(value)

    @classmethod
    def _decode_value(cls, buf, strict=True):
        return decode_int(buf)


class ASN1String(Simple):
    """Base class for string types."""

    @property
    def value(self):
        return self._value

    def _encode_value(self):
        return self._value

    @classmethod
    def _convert_value(cls, value):
        if isinstance(value, str) or isinstance(value, buffer):
            return str(value)
        elif isinstance(value, ASN1String):
            return value.value
        else:
            raise TypeError("Cannot convert %s to %s" %
                            (type(value), cls.__name__))

    @classmethod
    def _decode_value(cls, buf, strict=True):
        return buf


# TODO(ekasper): character sets
@Universal(19, tag.PRIMITIVE)
class PrintableString(ASN1String):
    """PrintableString."""
    pass


@Universal(20, tag.PRIMITIVE)
class TeletexString(ASN1String):
    """TeletexString (aka T61String)."""
    pass


@Universal(22, tag.PRIMITIVE)
class IA5String(ASN1String):
    """IA5String."""
    pass


@Universal(30, tag.PRIMITIVE)
class BMPString(ASN1String):
    """BMPString."""
    pass


@Universal(12, tag.PRIMITIVE)
class UTF8String(ASN1String):
    """UTF8String."""
    pass


@Universal(28, tag.PRIMITIVE)
class UniversalString(ASN1String):
    """UniversalString."""
    pass


@Universal(23, tag.PRIMITIVE)
class UTCTime(ASN1String):
    """UTCTime."""
    pass


@Universal(24, tag.PRIMITIVE)
class GeneralizedTime(ASN1String):
    """GeneralizedTime."""
    pass


@Universal(4, tag.PRIMITIVE)
class OctetString(ASN1String):
    """Octet string."""
    pass


@Universal(3, tag.PRIMITIVE)
class BitString(Abstract):
    """Bit string."""

    def __hash__(self):
        return hash(self._value)

    @property
    def value(self):
        """The value of a BitString is a string of '0's and '1's."""
        return self._value

    def _encode_value(self):
        pad = (8 - len(self._value) % 8) % 8
        padded_bits = self._value + pad*"0"
        ret = bytearray([pad])
        for i in range(0, len(padded_bits), 8):
            ret.append(int(padded_bits[i:i+8], 2))
        return str(ret)

    def _convert_value(self, value):
        if isinstance(value, BitString):
            return value.value
        elif isinstance(value, str):
            # Must be a string of '0's and '1's.
            if not all(c == "0" or c == "1" for c in value):
                raise ValueError("Cannot initialize a BitString from %s:"
                                 "string must consist of 0s and 1s" % value)
            return value
        else:
            raise TypeError("Cannot initialize a BitString from %s"
                            % type(value))

    @classmethod
    def _decode_value(cls, buf, strict=True):
        if not buf:
            raise error.ASN1Error("Invalid encoding: empty %s value" %
                                  cls.__name__)
        int_bytes = bytearray(buf)
        pad = int_bytes[0]
        if pad > 7:
            raise error.ASN1Error("Invalid padding %d in %s" %
                                  (pad, cls.__name__))
        ret = "".join(format(b, "08b") for b in int_bytes[1:])
        if pad:
            if not ret or any([c == "1" for c in ret[-1*pad:]]):
                raise error.ASN1Error("Invalid padding")
            ret = ret[:-1*pad]
        return ret


class Any(Abstract):
    """Any.

    Any is a container for an arbitrary value. An Any type can be tagged with
    explicit tags like any other type: those tags will be applied to the
    underlying value. Implicit tagging of Any types is not supported.

    Any can hold both decoded and undecoded values. Undecoded values are stored
    as raw strings.
    """
    def __init__(self, value=None, serialized_value=None, strict=True):
        if isinstance(value, str):
            super(Any, self).__init__(value=None, serialized_value=value,
                                      strict=strict)
            self._decoded_value = None
        else:
            super(Any, self).__init__(value=value,
                                      serialized_value=serialized_value,
                                      strict=strict)
            self._decoded_value = value

    def __repr__(self):
        if self._decoded_value is not None:
            return "%s(%r)" % (self.__class__.__name__, self._decoded_value)
        return "%s(%r)" % (self.__class__.__name__, self._value)

    def __str__(self):
        if self._decoded_value is not None:
            return str(self._decoded_value)
        return self._value

    def __hash__(self):
        return hash(self._value)

    @property
    def value(self):
        """The undecoded value."""
        # Always return the undecoded value for consistency; the
        # decoded/decoded_value properties can be used to retrieve the
        # decoded contents.
        return self._value

    @property
    def decoded(self):
        return self._decoded_value is not None

    @property
    def decoded_value(self):
        return self._decoded_value

    def _encode_value(self):
        return self._value

    @classmethod
    def _read(cls, buf, strict=True):
       _, rest = tag.Tag.read(buf)
       length, rest = read_length(rest)
       if len(rest) < length:
           raise error.ASN1Error("Invalid length encoding")
       decoded_length = len(buf) - len(rest) + length
       return cls(serialized_value=buf[:decoded_length],
                  strict=strict), buf[decoded_length:]

    @classmethod
    def _convert_value(cls, value):
        if isinstance(value, Any):
            # This gets ambiguous real fast (do we keep the original tags or
            # replace with our own tags?) so we ban it.
            raise TypeError("Instantiating Any from another Any is illegal")
        elif isinstance(value, Abstract):
            return value.encode()
        else:
            raise TypeError("Cannot convert %s to %s" % (type(value),
                            cls.__name__))

    @classmethod
    def _decode_value(cls, buf, strict=True):
        return buf

    def decode_inner(self, value_type, strict=True):
        """Decode the undecoded contents according to a given specification.

        Args:
            value_type: an ASN.1 type.
            strict: if False, tolerate some non-fatal decoding errors.

        Raises:
            ASN1Error: decoding failed.
            RuntimeError: value already decoded.
        """
        self._decoded_value = value_type.decode(self._value, strict=strict)


class MetaChoice(abc.ABCMeta):
    """Metaclass for building a Choice type."""

    def __new__(mcs, name, bases, dic):
        # Build a tag -> component_name map for the decoder.
        components = dic.get("components", {})
        if components:
            tag_map = {}
            keys_seen = set()
            for key, spec in components.iteritems():
                if key in keys_seen:
                    raise TypeError("Duplicate name in Choice specification")
                keys_seen.add(key)

                if not spec.tags:
                    raise TypeError("Choice type cannot have untagged "
                                    "components")
                if spec.tags[-1] in tag_map:
                    raise TypeError("Duplicate outer tag in a Choice "
                                    "specification")
                tag_map[spec.tags[-1]] = key
            dic["tag_map"] = tag_map
        return super(MetaChoice, mcs).__new__(mcs, name, bases, dic)


class Choice(Abstract, collections.MutableMapping):
    """Choice."""
    __metaclass__ = MetaChoice

    def __init__(self, value=None, serialized_value=None,
                 readahead_tag=None, readahead_value=None, strict=True):
        """Initialize fully or partially.

        Args:
            value: if present, should be a dictionary with one entry
                representing the chosen key and value.
            serialized_value: if present, the serialized contents (with tags
                and lengths stripped).
            readahead_tag: if present, the first tag in serialized_value
            readahead_value: if present, the value wrapped by the first tag in
                serialized value.
            strict: if False, tolerate some non-fatal decoding errors.

        Raises:
            ValueError: invalid initializer value.
        """
        if readahead_tag is not None:
            self._value = self._decode_readahead_value(
                serialized_value, readahead_tag, readahead_value,
                strict=strict)
        else:
            super(Choice, self).__init__(value=value,
                                         serialized_value=serialized_value,
                                         strict=strict)

    def __getitem__(self, key):
        value = self._value.get(key, None)
        if value is not None:
            return value
        elif key in self.components:
            return None
        raise KeyError("Invalid key %s for %s" % (key, self.__class__.__name__))

    def __setitem__(self, key, value):
        spec = self.components[key]
        if value is None:
            self._value = {}
        elif type(value) is spec:
            self._value = {key: value}
        # If the supplied value is not of the exact same type then we try to
        # construct one.
        else:
            self._value = {key: spec(value)}

    def __delitem__(self, key):
        if key in self._value:
            self._value = {}
        # Raise if the key is invalid; else do nothing.
        elif key not in self.components:
            raise KeyError("Invalid key %s" % key)

    def __iter__(self):
        return iter(self._value)

    def __len__(self):
        return len(self._value)

    @property
    def value(self):
        return dict(self._value)

    def _encode_value(self):
        if not self._value:
            raise error.ASN1Error("Choice component not set")
        # Encode the single component.
        return self._value.values()[0].encode()

    @classmethod
    def _read(cls, buf, strict=True):
        readahead_tag, rest = tag.Tag.read(buf)
        length, rest = read_length(rest)
        if len(rest) < length:
            raise error.ASN1Error("Invalid length encoding")
        decoded_length = len(buf) - len(rest) + length
        return (cls(serialized_value=buf[:decoded_length],
                    readahead_tag=readahead_tag, readahead_value=rest[:length],
                    strict=strict),
                buf[decoded_length:])

    @classmethod
    def _convert_value(cls, value):
        if not value:
            return dict()
        if len(value) != 1:
            raise ValueError("Choice must have at most one component set")

        key, value = value.iteritems().next()
        if value is None:
            return {}

        try:
            spec = cls.components[key]
        except KeyError:
            raise ValueError("Invalid Choice key %s" % key)
        if type(value) is spec:
            return {key: value}
        # If the supplied value is not of the exact same type then we try to
        # construct one.
        else:
            return {key: spec(value)}

    @classmethod
    def _decode_readahead_value(cls, buf, readahead_tag, readahead_value,
                                strict=True):
        """Decode using additional information about the outermost tag."""
        try:
            key = cls.tag_map[readahead_tag]
        except KeyError:
            raise error.ASN1TagError("Tag %s is not a valid tag for a "
                                     "component of %s" %
                                     (readahead_tag, cls.__name__))

        if len(cls.components[key].tags) == 1:
            # Shortcut: we already know the tag and length, so directly get
            # the value.
            value = cls.components[key](serialized_value=readahead_value)
        else:
            # Component has multiple tags but the readahead only read the
            # outermost tag, so read everything again.
            value, rest = cls.components[key].read(buf, strict=strict)
            if rest:
                raise error.ASN1Error("Invalid encoding: leftover bytes when "
                                      "decoding %s" % cls.__name__)
        return {key: value}

    @classmethod
    def _decode_value(cls, buf, strict=True):
        readahead_tag, rest = tag.Tag.read(buf)
        length, rest = read_length(rest)
        if len(rest) != length:
            raise error.ASN1Error("Invalid length encoding")
        return cls._decode_readahead_value(buf, readahead_tag, rest,
                                           strict=strict)


class Repeated(Abstract, collections.MutableSequence):
    """Base class for SetOf and SequenceOf."""

    def __getitem__(self, index):
        return self._value[index]

    def __setitem__(self, index, value):
        # We are required to support both single-value as well as slice
        # assignment.
        if isinstance(index, slice):
            self._value[index] = self._convert_value(v)
        else:
            self._value[index] = (value if type(value) is self.component
                                  else self.component(value))

    def __delitem__(self, index):
        del self._value[index]

    def __len__(self):
        return len(self._value)

    def insert(self, index, value):
        if type(value) is not self.component:
            value = self.component(value)
        self._value.insert(index, value)

    @property
    def value(self):
        return list(self._value)

    @classmethod
    def _convert_value(cls, value):
        return [x if type(x) is cls.component else cls.component(x)
                for x in value]


@Universal(16, tag.CONSTRUCTED)
class SequenceOf(Repeated):
    """Sequence Of."""
    def _encode_value(self):
        ret = [x.encode() for x in self._value]
        return "".join(ret)

    @classmethod
    def _decode_value(cls, buf, strict=True):
        ret = []
        while buf:
            value, buf = cls.component.read(buf, strict=strict)
            ret.append(value)
        return ret


# We cannot use a real set to represent SetOf because
# (a) our components are mutable and thus not hashable and
# (b) ASN.1 allows duplicates: {1} and {1, 1} are distinct sets.
# Note that this means that eq-comparison is order-dependent.
@Universal(17, tag.CONSTRUCTED)
class SetOf(Repeated):
    """Set Of."""

    def _encode_value(self):
        ret = [x.encode() for x in self._value]
        ret.sort()
        return "".join(ret)

    @classmethod
    def _decode_value(cls, buf, strict=True):
        ret = []
        while buf:
            value, buf = cls.component.read(buf, strict=strict)
            ret.append(value)
        # TODO(ekasper): reject BER encodings in strict mode, i.e.,
        # verify sort order.
        return ret


class Component(object):
    """Sequence component specification."""

    def __init__(self, name, value_type, optional=False, default=None,
                 defined_by=None, lookup=None):
        """Define a sequence component.

        Args:
            name: component name. Must be unique within a sequence.
            value_type: the ASN.1 type.
            optional: if True, the component is optional.
            default: default value of the component.
            defined_by: for Any types, this specifies the component
                that defines the type.
            lookup: the lookup dictionary for Any types.
        """
        self.name = name
        self.value_type = value_type
        if default is None or type(default) is value_type:
            self.default = default
        else:
            self.default = value_type(default)
        if self.default is not None:
            self.encoded_default = self.default.encode()
        else:
            self.encoded_default = None
        self.optional = optional or (self.default is not None)
        self.defined_by = defined_by
        self.lookup = lookup


class MetaSequence(abc.ABCMeta):
    """Metaclass for building Sequence types."""

    def __new__(mcs, name, bases, dic):
        # Build a key -> component map for setting values.
        components = dic.get("components", ())
        if components:
            key_map = {}
            for component in components:
                if component.name in key_map:
                    raise TypeError("Duplicate name in Sequence specification")
                key_map[component.name] = component
            dic["key_map"] = key_map
        return super(MetaSequence, mcs).__new__(mcs, name, bases, dic)


@Universal(16, tag.CONSTRUCTED)
class Sequence(Abstract, collections.MutableMapping):
    """Sequence."""
    __metaclass__ = MetaSequence

    def __getitem__(self, key):
        return self._value[key]

    def __setitem__(self, key, value):
        component = self.key_map[key]
        value = self._convert_single_value(component, value)
        self._values[key] = value

    def __delitem__(self, key):
        if key not in self.key_map:
            raise KeyError("Invalid key %s" % key)
        self[key] = None

    def __iter__(self):
        """Iterate component names in order."""
        for component in self.components:
            yield component.name

    def __len__(self):
        """Missing optional components are counted in the length."""
        return len(self.components)

    @property
    def value(self):
        # Note that this does not preserve the component order.
        # However an order is encoded in the type spec, so we can still
        # recreate the original object from this value.
        return dict(self._value)

    def _encode_value(self):
        ret = []
        for component in self.components:
            value = self._value[component.name]
            if value is None:
                if not component.optional:
                    raise error.ASN1Error("Missing %s value in %s" %
                                          (component.name,
                                           self.__class__.__name__))
            else:
                # Value is not None.
                # We could compare by value for most types, but for "set" types
                # different values may yield the same encoding, so we compare
                # directly by encoding.
                # (Even though I haven't seen a defaulted set type in practice.)
                encoded_value = value.encode()
                if component.encoded_default != encoded_value:
                    ret.append(encoded_value)
        return "".join(ret)

    @classmethod
    def _convert_single_value(cls, component, value):
        # If value is None, we store the default if it is different from None.
        if value is None:
            return component.default
        elif type(value) is component.value_type:
            return value
        # If the supplied value is not of the exact same type then we discard
        # the tag information and try to construct from scratch.
        else:
            # TODO(ekasper): verify defined_by constraints here.
            return component.value_type(value)

    @classmethod
    def _convert_value(cls, value):
        ret = {}
        value = value or {}
        if not all([key in cls.key_map for key in value]):
            raise ValueError("Invalid keys in initializer")
        for component in cls.components:
            ret[component.name] = cls._convert_single_value(
                component, value.get(component.name, None))
        return ret

    @classmethod
    def _decode_value(cls, buf, strict=True):
        ret = dict()
        for component in cls.components:
            try:
                value, buf = component.value_type.read(buf, strict=strict)
            except error.ASN1TagError:
                # If the component was optional and we got a tag mismatch,
                # assume decoding failed because the component was missing,
                # and carry on.
                # TODO(ekasper): since we let errors fall through recursively,
                # not all of the tag errors can be reasonably explained by
                # missing optional components. We could tighten this to match by
                # outermost tag only, and have metaclass verify the uniqueness
                # of component tags. Meanwhile, the worst that can happen is
                # that we retry in vain and don't return the most helpful error
                # message when we do finally fail.
                if not component.optional:
                    raise
                else:
                    ret[component.name] = component.default
            else:
                ret[component.name] = value
        if buf:
            raise error.ASN1Error("Invalid encoding")

        # Second pass for decoding ANY.
        for component in cls.components:
            if component.defined_by is not None:
                value_type = component.lookup.get(
                    ret[component.defined_by], None)
                if value_type is not None:
                    try:
                        ret[component.name].decode_inner(value_type,
                                                         strict=strict)
                    except error.ASN1Error:
                        if strict:
                            raise
        return ret
