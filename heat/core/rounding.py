import torch
from typing import Tuple, Optional, Callable
from .dndarray import DNDarray
from .types import datatype

from . import _operations
from . import dndarray
from . import types

__all__ = ["abs", "absolute", "ceil", "clip", "fabs", "floor", "modf", "round", "trunc"]


def abs(x: DNDarray, out: Optional[DNDarray] = None, dtype: Optional[datatype] = None) -> DNDarray:
    """
    Calculate the absolute value element-wise.

    Parameters
    ----------
    x : DNDarray
        The array for which the compute the absolute value.
    out : DNDarray, optional
        A location into which the result is stored. If provided, it must have a shape that the inputs broadcast to.
        If not provided or ``None``, a freshly-allocated array is returned.
    dtype : datatype, optional
        Determines the data type of the output array. The values are cast to this type with potential loss of
        precision.
    """
    if dtype is not None and not issubclass(dtype, dtype):
        raise TypeError("dtype must be a heat data type")

    absolute_values = _operations.__local_op(torch.abs, x, out)
    if dtype is not None:
        absolute_values._DNDarray__array = absolute_values._DNDarray__array.type(dtype.torch_type())
        absolute_values._DNDarray__dtype = dtype

    return absolute_values


DNDarray.abs: Callable[
    [DNDarray, Optional[DNDarray], Optional[datatype]], DNDarray
] = lambda self, out=None, dtype=None: abs(self, out, dtype)
DNDarray.abs.__doc__ = abs.__doc__


def absolute(
    x: DNDarray, out: Optional[DNDarray] = None, dtype: Optional[datatype] = None
) -> DNDarray:
    """
    Calculate the absolute value element-wise.
    :func:`abs` is a shorthand for this function.

    Parameters
    ----------
    x : DNDarray
        The array for which the compute the absolute value.
    out : DNDarray, optional
        A location into which the result is stored. If provided, it must have a shape that the inputs broadcast to.
        If not provided or ``None``, a freshly-allocated array is returned.
    dtype : datatype, optional
        Determines the data type of the output array. The values are cast to this type with potential loss of
        precision.
    """
    return abs(x, out, dtype)


DNDarray.absolute: Callable[
    [DNDarray, Optional[DNDarray], Optional[datatype]], DNDarray
] = lambda self, out=None, dtype=None: absolute(self, out, dtype)
DNDarray.absolute.__doc__ = absolute.__doc__


def ceil(x: DNDarray, out: Optional[DNDarray] = None) -> DNDarray:
    """
    Return the ceil of the input, element-wise. Result is a ``DNDarray`` of the same shape as ``x``.
    The ceil of the scalar ``x`` is the smallest integer i, such that ``i>=x``. It is often denoted as :math:`\\lceil x \\rceil`.

    Parameters
    ----------
    x : DNDarray
        The value for which to compute the ceiled values.
    out : DNDarray, optional
        A location in which to store the results. If provided, it must have a broadcastable shape. If not provided
        or set to ``None``, a fresh array is allocated.

    Examples
    --------
    >>> ht.ceil(ht.arange(-2.0, 2.0, 0.4))
    tensor([-2., -1., -1., -0., -0., -0.,  1.,  1.,  2.,  2.])
    """
    return _operations.__local_op(torch.ceil, x, out)


DNDarray.ceil: Callable[[DNDarray, Optional[DNDarray]], DNDarray] = lambda self, out=None: ceil(
    self, out
)
DNDarray.ceil.__doc__ = ceil.__doc__


def clip(x: DNDarray, min, max, out: Optional[DNDarray] = None) -> DNDarray:
    """
    Returns a :class:`~heat.core.dndarray.DNDarray` with the elements of this array, but where values ``<a_min`` are replaced with ``a_min``, and those
    ``>a_max`` with ``a_max``.

    Parameters
    ----------
    x : DNDarray
        Array containing elements to clip.
    min : scalar or None
        Minimum value. If ``None``, clipping is not performed on lower interval edge. Not more than one of ``a_min`` and
        ``a_max`` may be ``None``.
    max : scalar or None
        Maximum value. If ``None``, clipping is not performed on upper interval edge. Not more than one of ``a_min`` and
        ``a_max`` may be None.
    out : DNDarray, optional
        The results will be placed in this array. It may be the input array for in-place clipping. ``out`` must be of
        the right shape to hold the output. Its type is preserved.
    """
    if not isinstance(x, DNDarray):
        raise TypeError("a must be a DNDarray")
    if min is None and max is None:
        raise ValueError("either min or max must be set")

    return _operations.__local_op(torch.clamp, x, out, min=min, max=max)


DNDarray.clip = lambda self, a_min, a_max, out=None: clip(self, a_min, a_max, out)
DNDarray.clip.__doc__ = clip.__doc__


def fabs(x: DNDarray, out: Optional[DNDarray] = None) -> DNDarray:
    """
    Calculate the absolute value element-wise and return floating-point ``DNDarray``.
    This function exists besides ``abs==absolute`` since it will be needed in case complex numbers will be introduced in the future.

    Parameters
    ----------
    x : DNDarray
        The array for which the compute the absolute value.
    out : DNDarray, optional
        A location into which the result is stored. If provided, it must have a shape that the inputs broadcast to.
        If not provided or ``None``, a freshly-allocated array is returned.
    """

    return abs(x, out, dtype=None)


DNDarray.fabs: Callable[[DNDarray, Optional[DNDarray]], DNDarray] = lambda self, out=None: fabs(
    self, out
)
DNDarray.fabs.__doc__ = fabs.__doc__


def floor(x: DNDarray, out: Optional[DNDarray] = None) -> DNDarray:
    """
    Return the floor of the input, element-wise.
    The floor of the scalar ``x`` is the largest integer i, such that ``i<=x``.
    It is often denoted as :math:`\\lfloor x \\rfloor`.

    Parameters
    ----------
    x : DNDarray
        The array for which to compute the floored values.
    out : DNDarray, optional
        A location in which to store the results. If provided, it must have a broadcastable shape. If not provided
        or set to ``None``, a fresh ``DNDarray`` is allocated.

    Examples
    --------
    >>> ht.floor(ht.arange(-2.0, 2.0, 0.4))
    tensor([-2., -2., -2., -1., -1.,  0.,  0.,  0.,  1.,  1.])
    """
    return _operations.__local_op(torch.floor, x, out)


DNDarray.floor: Callable[[DNDarray, Optional[DNDarray]], DNDarray] = lambda self, out=None: floor(
    self, out
)
DNDarray.floor.__doc__ = floor.__doc__


def modf(x: DNDarray, out: Optional[Tuple[DNDarray, DNDarray]] = None) -> Tuple[DNDarray, DNDarray]:
    """
    Return the fractional and integral parts of a ``DNDarray``, element-wise.
    The fractional and integral parts are negative if the given number is negative.

    Parameters
    ----------
    x : DNDarray
        Input array
    out : Tuple[DNDarray, DNDarray], optional
        A location into which the result is stored. If provided, it must have a shape that the inputs broadcast to.
        If not provided or ``None``, a freshly-allocated array is returned.

    Examples
    --------
    >>> ht.modf(ht.arange(-2.0, 2.0, 0.4))
    (tensor([-2., -1., -1., -0., -0.,  0.,  0.,  0.,  1.,  1.]),
    tensor([ 0.0000, -0.6000, -0.2000, -0.8000, -0.4000,  0.0000,  0.4000,  0.8000, 0.2000,  0.6000]))
    """
    if not isinstance(x, DNDarray):
        raise TypeError("expected x to be a ht.DNDarray, but was {}".format(type(x)))

    integralParts = trunc(x)
    fractionalParts = x - integralParts

    if out is not None:
        if not isinstance(out, tuple):
            raise TypeError(
                "expected out to be None or a tuple of ht.DNDarray, but was {}".format(type(out))
            )
        if len(out) != 2:
            raise ValueError(
                "expected out to be a tuple of length 2, but was of length {}".format(len(out))
            )
        if (not isinstance(out[0], DNDarray)) or (not isinstance(out[1], DNDarray)):
            raise TypeError(
                "expected out to be None or a tuple of ht.DNDarray, but was ({}, {})".format(
                    type(out[0]), type(out[1])
                )
            )
        out[0]._DNDarray__array = fractionalParts._DNDarray__array
        out[1]._DNDarray__array = integralParts._DNDarray__array
        return out

    return (fractionalParts, integralParts)


DNDarray.modf: Callable[
    [DNDarray, Optional[Tuple[DNDarray, DNDarray]]], Tuple[DNDarray, DNDarray]
] = lambda self, out=None: modf(self, out)
DNDarray.modf.__doc__ = modf.__doc__


def round(
    x: DNDarray, decimals: int = 0, out: Optional[DNDarray] = None, dtype: Optional[datatype] = None
) -> DNDarray:
    """
    Calculate the rounded value element-wise.

    Parameters
    ----------
    x : DNDarray
        The array for which the compute the rounded value.
    decimals: int, optional
        Number of decimal places to round to.
        If decimals is negative, it specifies the number of positions to the left of the decimal point.
    out : DNDarray, optional
        A location into which the result is stored. If provided, it must have a shape that the inputs broadcast to.
        If not provided or ``None``, a freshly-allocated array is returned.
    dtype : datatype, optional
        Determines the data type of the output array. The values are cast to this type with potential loss of
        precision.

    Examples
    --------
    >>> ht.round(ht.arange(-2.0, 2.0, 0.4))
    tensor([-2., -2., -1., -1., -0.,  0.,  0.,  1.,  1.,  2.])

    """
    if dtype is not None and not issubclass(dtype, datatype):
        raise TypeError("dtype must be a heat data type")

    if decimals != 0:
        x *= 10 ** decimals

    rounded_values = _operations.__local_op(torch.round, x, out)

    if decimals != 0:
        rounded_values /= 10 ** decimals

    if dtype is not None:
        rounded_values._DNDarray__array = rounded_values._DNDarray__array.type(dtype.torch_type())
        rounded_values._DNDarray__dtype = dtype

    return rounded_values


DNDarray.round: Callable[
    [DNDarray, int, Optional[DNDarray], Optional[datatype]], DNDarray
] = lambda self, decimals=0, out=None, dtype=None: round(self, decimals, out, dtype)
DNDarray.round.__doc__ = round.__doc__


def trunc(x: DNDarray, out: Optional[DNDarray] = None) -> DNDarray:
    """
    Return the trunc of the input, element-wise.
    The truncated value of the scalar ``x`` is the nearest integer ``i`` which is closer to zero than ``x`` is. In short, the
    fractional part of the signed number ``x`` is discarded.

    Parameters
    ----------
    x : DNDarray
        The array for which to compute the trunced values.
    out : DNDarray, optional
        A location in which to store the results. If provided, it must have a broadcastable shape. If not provided
        or set to ``None``, a fresh array is allocated.

    Examples
    --------
    >>> ht.trunc(ht.arange(-2.0, 2.0, 0.4))
    tensor([-2., -1., -1., -0., -0.,  0.,  0.,  0.,  1.,  1.])
    """
    return _operations.__local_op(torch.trunc, x, out)


DNDarray.trunc: Callable[[DNDarray, Optional[DNDarray]], DNDarray] = lambda self, out=None: trunc(
    self, out
)
DNDarray.trunc.__doc__ = trunc.__doc__
