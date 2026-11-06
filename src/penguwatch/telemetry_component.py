"""The telemetry sampler component: event-loop lag and the state gauges. Optional, and only with a meter provider."""

from typing import Optional

from penguwatch import telemetry
from penguwatch.components import Component, Context

OPTIONAL = True


async def register(ctx: Context) -> Optional[Component]:
    if ctx.otel is None or ctx.otel.meter_provider is None:
        return None

    async def start() -> None:
        ctx.spawn(telemetry.sample_loop(ctx.container))

    return Component("telemetry", start=start)
