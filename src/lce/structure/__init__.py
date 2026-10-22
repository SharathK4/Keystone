"""Structural results that sit beside the analytical snapshot.

The first is the **value of speed**: how much of a plan's measured effect
survives when the money lands later. Built offline (``build``), stored as a
content-addressed artifact, and served read-only (``store``). Serving never
imports the builder, the dataset generator or the benchmark.
"""
