"""This module provides a workaround to allow callback functions to return composite types (most importantly structs).

Currently, ctypes callback functions (created by passing a Python callable to a CFUNCTYPE object) are only able to
return what ctypes considers a "simple" type. This includes void (None), scalars (c_int, c_float, etc.), c_void_p,
c_char_p, c_wchar_p, and py_object. Returning "composite" types (structs, unions, and non-"simple" pointers) is
not possible. This issue has been reported on the Python bug tracker as bpo-5710 (https://bugs.python.org/issue5710).

For pointers, the easiest workaround is to return a c_void_p instead of the correctly typed pointer, and to cast
the value on both sides. For structs and unions there is no easy workaround, which is why this somewhat hacky
workaround is necessary.
"""

import ctypes
import sys
import warnings


# This module relies on the layout of a few internal Python and ctypes structures.
# Because of this, it's possible (but not all that likely) that things will break on newer/older Python versions.
if sys.version_info < (3, 4) or sys.version_info >= (3, 7):
    warnings.warn(
        "rubicon.objc.ctypes_patch has only been tested with Python 3.4 through 3.6. "
        "The current version is {}. Most likely things will work properly, "
        "but you may experience crashes if Python's internals have changed significantly."
        .format(sys.version_info)
    )


# The PyTypeObject struct from "Include/object.h".
# This is a forward declaration, fields are set later once PyVarObject has been declared.
class PyTypeObject(ctypes.Structure):
    pass


# The PyObject struct from "Include/object.h".
class PyObject(ctypes.Structure):
    _fields_ = [
        ("ob_refcnt", ctypes.c_ssize_t),
        ("ob_type", ctypes.POINTER(PyTypeObject)),
    ]


# The PyVarObject struct from "Include/object.h".
class PyVarObject(ctypes.Structure):
    _fields_ = [
        ("ob_base", PyObject),
        ("ob_size", ctypes.c_ssize_t),
    ]


# This structure is not stable across Python versions, but the few fields that we use probably won't change.
PyTypeObject._fields_ = [
    ("ob_base", PyVarObject),
    ("tp_name", ctypes.c_char_p),
    ("tp_basicsize", ctypes.c_ssize_t),
    ("tp_itemsize", ctypes.c_ssize_t),
    # There are many more fields, but we're only interested in the size fields, so we can leave out everything else.
]


# The PyTypeObject structure for the dict class.
# This is used to determine the size of the PyDictObject structure.
PyDict_Type = PyTypeObject.from_address(id(dict))


# The PyDictObject structure from "Include/dictobject.h".
# This structure is not stable across Python versions, and did indeed change in recent Python releases.
# Because we only care about the size of the structure and not its actual contents,
# we can declare it as an opaque byte array, with the length taken from PyDict_Type.
class PyDictObject(ctypes.Structure):
    _fields_ = [
        ("PyDictObject_opaque", (ctypes.c_ubyte*PyDict_Type.tp_basicsize)),
    ]


# The ffi_type structure from libffi's "include/ffi.h".
# This is a forward declaration, because the structure contains pointers to itself.
class ffi_type(ctypes.Structure):
    pass


ffi_type._fields_ = [
    ("size", ctypes.c_size_t),
    ("alignment", ctypes.c_ushort),
    ("type", ctypes.c_ushort),
    ("elements", ctypes.POINTER(ctypes.POINTER(ffi_type))),
]


# The GETFUNC and SETFUNC typedefs from "Modules/_ctypes/ctypes.h".
GETFUNC = ctypes.PYFUNCTYPE(ctypes.py_object, ctypes.c_void_p, ctypes.c_ssize_t)
SETFUNC = ctypes.PYFUNCTYPE(ctypes.py_object, ctypes.c_void_p, ctypes.py_object, ctypes.c_ssize_t)


# The StgDictObject structure from "Modules/_ctypes/ctypes.h".
# This structure is not officially stable across Python versions,
# but it basically hasn't changed since ctypes was originally added to Python in 2009.
class StgDictObject(ctypes.Structure):
    _fields_ = [
        ("dict", PyDictObject),
        ("size", ctypes.c_ssize_t),
        ("align", ctypes.c_ssize_t),
        ("length", ctypes.c_ssize_t),
        ("ffi_type_pointer", ffi_type),
        ("proto", ctypes.py_object),
        ("setfunc", SETFUNC),
        ("getfunc", GETFUNC),
        # There are a few more fields, but we leave them out again because we don't need them.
    ]


# The PyObject_stgdict function from "Modules/_ctypes/ctypes.h".
ctypes.pythonapi.PyType_stgdict.restype = ctypes.POINTER(StgDictObject)
ctypes.pythonapi.PyType_stgdict.argtypes = [ctypes.py_object]


def make_callback_returnable(ctype):
    """Modify the given ctypes type so it can be returned from a callback function.

    This function may be used as a decorator on a struct/union declaration.
    """

    # Extract the StgDict from the ctype.
    stgdict_c = ctypes.pythonapi.PyType_stgdict(ctype).contents

    # Ensure that there is no existing getfunc or setfunc on the stgdict.
    if ctypes.cast(stgdict_c.getfunc, ctypes.c_void_p).value is not None:
        raise ValueError("The ctype {} already has a getfunc")
    elif ctypes.cast(stgdict_c.setfunc, ctypes.c_void_p).value is not None:
        raise ValueError("The ctype {} already has a setfunc")

    # Define the getfunc and setfunc.
    @GETFUNC
    def getfunc(ptr, size):
        actual_size = ctypes.sizeof(ctype)
        if size != 0 and size != actual_size:
            raise ValueError(
                "getfunc for ctype {}: Requested size {} does not match actual size {}"
                .format(ctype, size, actual_size)
            )

        return ctype.from_buffer_copy(ctypes.string_at(ptr, actual_size))

    @SETFUNC
    def setfunc(ptr, value, size):
        actual_size = ctypes.sizeof(ctype)
        if size != 0 and size != actual_size:
            raise ValueError(
                "setfunc for ctype {}: Requested size {} does not match actual size {}"
                .format(ctype, size, actual_size)
            )

        ctypes.memmove(ptr, ctypes.addressof(value), actual_size)
        return None

    # Store the getfunc and setfunc as attributes on the ctype, so they don't get garbage-collected.
    ctype._rubicon_objc_ctypes_patch_getfunc = getfunc
    ctype._rubicon_objc_ctypes_patch_setfunc = setfunc
    # Put the getfunc and setfunc into the stgdict fields.
    stgdict_c.getfunc = getfunc
    stgdict_c.setfunc = setfunc

    # Return the passed in ctype, so this function can be used as a decorator.
    return ctype
