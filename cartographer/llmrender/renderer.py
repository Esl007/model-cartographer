"""Phase 4: LLM natural-language rendering of a `World` into a story.

`render_llm` asks a "renderer" model to narrate a structured fact list as
prose. The story it returns never includes the question — `assemble_prompt`
appends the same question text `templates.render()` would produce, so the
final task presented to a subject model is comparable across render modes.
"""

from __future__ import annotations

import json

from ..exec.client import Client
from ..render.naming import NameMap
from ..render.templates import render as template_render
from ..world.model import World
from .facts import world_facts

RENDER_PROMPT_TEMPLATE = """You will receive structured facts describing a short scenario, as a JSON \
array. Write a natural-language story that narrates ALL of these facts, in \
any order and style you like.

Rules:
- Every sentence describing a fact MUST include its exact time anchor, \
formatted like "At 8:04, ...".
- Narrate every fact in the list. Do not omit any.
- If a fact has a non-empty "witnesses" list, explicitly state that each \
named witness was present/observed the event — do not silently drop them.
- Do not invent or add any event that is not in the list, and do not invent \
witnesses beyond those listed.
- Do not reveal, resolve, or comment on what any character believes, knows, \
or is thinking — narrate only what happened, in third person, without \
editorializing about anyone's mental state.
- Do not mention or hint at any question; write only the story.
- Sentence structure, phrasing, and ordering may vary freely as long as \
every fact is present with its exact time.

Facts (JSON array):
{facts_json}
"""


def render_llm_tokens(
    world: World,
    name_map: NameMap,
    client: Client,
    renderer_model: str,
    seed: int,
    temperature: float = 0.7,
) -> tuple[str | None, int]:
    """Like `render_llm`, but also reports total tokens spent on the call
    (0 if the call itself errored before any usage was billed)."""
    facts = world_facts(world, name_map)
    prompt = RENDER_PROMPT_TEMPLATE.format(facts_json=json.dumps(facts, indent=2))
    result = client.chat(
        model=renderer_model,
        prompt=prompt,
        max_tokens=2000,
        temperature=temperature,
    )
    tokens = result.tokens_in + result.tokens_out
    if result.error is not None:
        return None, tokens
    story = result.text.strip()
    if not story:
        return None, tokens
    return story, tokens


def render_llm(
    world: World,
    name_map: NameMap,
    client: Client,
    renderer_model: str,
    seed: int,
    temperature: float = 0.7,
) -> str | None:
    """Ask `renderer_model` to narrate `world`'s facts as prose. Returns the
    story text, or None if the client call errors."""
    story, _tokens = render_llm_tokens(
        world, name_map, client, renderer_model, seed=seed, temperature=temperature
    )
    return story


def assemble_prompt(story: str, world: World, name_map: NameMap) -> str:
    """Append the same question text `templates.render()` would produce for
    this world, to an LLM-rendered `story`.

    Reuses `templates.render()` directly (rather than reimplementing
    question logic) and splits the question off the template's own output:
    `templates.render()` joins story and question with a blank line
    ("{story}\\n\\n{question}"), so everything after the final blank line is
    the question text.
    """
    template_rendered = template_render(world, name_map, seed=0)
    question = template_rendered.prompt.rsplit("\n\n", 1)[-1]
    return f"{story}\n\n{question}"
