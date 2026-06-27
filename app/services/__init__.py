"""Use cases.

A service owns one entity's rules and is the only place that changes its
state. Routers call services; task handlers call services; nothing writes a
status field behind their back.

That is precisely the property the previous version lacked — job status was
recomputed in a router, in a task module, and in a scheduler, and the three
disagreed with each other.
"""
