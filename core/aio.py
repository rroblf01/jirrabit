"""Async wrappers for Django APIs that don't ship an async equivalent.

Only used for surfaces where Django offers no async counterpart:
- ``render()`` (template engine is sync-only)
- ``Form.is_valid()`` (no async form pipeline)
- ``Form.save_m2m()`` (no async helper)

Everything ORM-related uses the native async APIs (``aget``, ``asave``,
``aset``, ``async for``) and should NOT use these wrappers.
"""

from asgiref.sync import sync_to_async
from django.shortcuts import render

arender = sync_to_async(render)


async def avalid(form) -> bool:
    return await sync_to_async(form.is_valid)()


async def asave_m2m(form) -> None:
    await sync_to_async(form.save_m2m)()


async def aform(form_cls, *args, **kwargs):
    """Build a Form/ModelForm off the event loop.

    ``ModelForm(instance=obj)`` with M2M fields hits the database during
    ``__init__`` (to seed the initial values), and Django offers no async
    alternative for form construction.
    """
    return await sync_to_async(form_cls)(*args, **kwargs)
