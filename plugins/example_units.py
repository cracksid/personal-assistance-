"""
Example plugin: unit conversion.

Copy this file, rename it, and change the contents -- it is a complete,
working plugin and there is nothing else to wire up.

Why unit conversion rather than "hello world": language models are
genuinely unreliable at arithmetic, and confidently wrong is the worst
failure mode there is. Handing the sums to Python is exactly the kind of
job a tool exists for, which makes this a real example rather than a
demonstration of the plumbing.
"""

from app.plugins.sdk import BaseModel, Field, Tool, ToolContext, ToolResult

# Everything is converted through one base unit per kind. Adding a unit is
# one line here and nothing else -- a conversion table with an entry per
# pair would need 30 entries for these 6 units, and 90 for 10.
FACTORS: dict[str, tuple[str, float]] = {
    # length, base: metre
    "mm": ("length", 0.001),
    "cm": ("length", 0.01),
    "m": ("length", 1.0),
    "km": ("length", 1000.0),
    "in": ("length", 0.0254),
    "ft": ("length", 0.3048),
    "mi": ("length", 1609.344),
    # mass, base: kilogram
    "g": ("mass", 0.001),
    "kg": ("mass", 1.0),
    "lb": ("mass", 0.45359237),
    "oz": ("mass", 0.028349523125),
}


class ConvertInput(BaseModel):
    """
    The arguments, as a Pydantic model.

    The descriptions are not decoration: they are turned into JSON Schema
    and shown to the model, so they are how it learns what to pass. A vague
    description here produces bad tool calls.
    """

    value: float = Field(description="The number to convert, e.g. 12.5")
    from_unit: str = Field(description="Unit to convert from, e.g. 'km' or 'lb'.")
    to_unit: str = Field(description="Unit to convert to, e.g. 'mi' or 'kg'.")


class ConvertUnits(Tool):
    name = "convert_units"
    description = (
        "Convert a measurement between units of length (mm, cm, m, km, in, "
        "ft, mi) or mass (g, kg, lb, oz). Use this instead of calculating "
        "the conversion yourself."
    )
    input_schema = ConvertInput

    # Nothing is created, changed or deleted, so no confirmation. Set this
    # to True and the core will stop and ask before running -- the tool
    # itself never prompts.
    requires_confirmation = False

    def describe_action(self, args: ConvertInput) -> str:
        """What the user would read if this needed approval."""
        return f"Convert {args.value} {args.from_unit} to {args.to_unit}"

    async def run(self, args: ConvertInput, context: ToolContext) -> ToolResult:
        source = FACTORS.get(args.from_unit.strip().lower())
        target = FACTORS.get(args.to_unit.strip().lower())

        # Returning ok=False rather than raising: this is a refusal the model
        # can read and correct on its next attempt, not a failure.
        if source is None:
            return ToolResult(
                ok=False,
                error=f"I do not know the unit {args.from_unit!r}. "
                f"Known units: {', '.join(sorted(FACTORS))}.",
            )
        if target is None:
            return ToolResult(
                ok=False,
                error=f"I do not know the unit {args.to_unit!r}. "
                f"Known units: {', '.join(sorted(FACTORS))}.",
            )

        source_kind, source_factor = source
        target_kind, target_factor = target

        if source_kind != target_kind:
            return ToolResult(
                ok=False,
                error=f"{args.from_unit} measures {source_kind} and "
                f"{args.to_unit} measures {target_kind}. Those cannot be "
                "converted into each other.",
            )

        result = args.value * source_factor / target_factor

        # Output is text because it ends up in a prompt. :g drops trailing
        # zeros, so 5 km reads as "3.10686 mi" rather than "3.106860 mi".
        return ToolResult(
            output=f"{args.value:g} {args.from_unit} = {result:g} {args.to_unit}"
        )


def register() -> list[Tool]:
    """
    THE ENTRY POINT. The loader calls this and registers what it returns.

    Explicit rather than magic: the loader could scan this file for Tool
    subclasses, but then a class defined as a base, or imported for
    reference, would silently become a live tool. This says exactly what is
    on offer.
    """
    return [ConvertUnits()]
