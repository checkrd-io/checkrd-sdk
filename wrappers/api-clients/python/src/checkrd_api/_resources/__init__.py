"""Resource modules. One file per ``/v1/<resource>`` domain.

Resources are the pieces a user actually calls — ``client.agents``,
``client.policies``, etc. Each module exports a ``<Domain>`` class
(sync, used by :class:`Checkrd`) and an ``Async<Domain>`` class
(async, used by :class:`AsyncCheckrd`). Both have the same method
names; only the return-type annotations and ``await`` differ.
"""
