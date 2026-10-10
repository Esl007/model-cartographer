"""Evidence Fidelity Campaign transforms for the narrative (belief) family
(docs/spec-evidence-fidelity-campaign.md §3-§4, build notes §14).

Two prompt transforms over the SAME world, both answer-key-preserving by
construction (the world and the question are untouched; only story text is
repeated or flagged):

  * `witness_restatement` (condition C, salience): the belief-forming
    sentences are restated verbatim, with explicit names instead of
    pronouns, in a "Recall:" block immediately before the question;
  * `witness_emphasis` (condition D, presentation): the same sentences are
    flagged IN PLACE -- prefixed "Note:" and quoted -- with nothing added.

The belief-forming sentence set S is EVERY sentence (event sentence and its
witness sentence) of a Move or Transfer of the queried object that the
believer witnessed, plus every Tell to the believer about it, in narration
order -- not just the last one: picking the last belief-fixing event would
do the causal step ("which of these was last") for the model, and C must
carry the same information as B (second-opinion review, 2026-10-10). The
believer's absence from later events is never stated in B, so it is not
stated here either.

Not registered in `ablations.ABLATIONS` (that would change what the
attribution engine runs by default); the Pipeline evaluator applies these
only when its config carries the keys.
"""

from __future__ import annotations

from ..render.naming import NameMap
from ..render.templates import RenderedTask
from ..world.model import Move, Tell, Transfer, World
from .ablations import AblationUnavailable, _split_story_sentences, pronoun_suppression


class EvidenceUnavailable(AblationUnavailable):
    """This world has no belief-forming sentence to restate or flag."""


def belief_evidence_events(world: World) -> list[int]:
    """Event indices through which the queried believer saw or was told about
    the queried object."""
    q = world.query
    if q.kind != "belief" or q.agent is None:
        raise EvidenceUnavailable("not a belief query")
    out = []
    for i, e in enumerate(world.events):
        if isinstance(e, (Move, Transfer)) and e.obj == q.subject and q.agent in e.witnesses:
            out.append(i)
        elif isinstance(e, Tell) and e.obj == q.subject and e.listener == q.agent:
            out.append(i)
    return out


def belief_evidence_sentences(world: World, rendered: RenderedTask) -> list[int]:
    """Indices (into the rendered story's sentences) of the set S."""
    events = set(belief_evidence_events(world))
    idx = rendered.meta.get("sentence_event_idx", [])
    sentences = _split_story_sentences(rendered.prompt)
    if len(sentences) != len(idx):
        raise EvidenceUnavailable("sentence count does not match render() meta")
    hits = [i for i, e in enumerate(idx) if e in events]
    if not hits:
        raise EvidenceUnavailable("no belief-forming sentence")
    return hits


def witness_restatement(world: World, name_map: NameMap, rendered: RenderedTask, seed: int) -> str:
    hits = belief_evidence_sentences(world, rendered)
    plain = _split_story_sentences(pronoun_suppression(world, name_map, seed).prompt)
    story, question = rendered.prompt.rsplit("\n\n", 1)
    recall = "Recall:\n" + "\n".join(plain[i] for i in hits)
    return f"{story}\n\n{recall}\n\n{question}"


def witness_emphasis(world: World, name_map: NameMap, rendered: RenderedTask, seed: int) -> str:
    hits = set(belief_evidence_sentences(world, rendered))
    sentences = _split_story_sentences(rendered.prompt)
    question = rendered.prompt.rsplit("\n\n", 1)[1]
    story = " ".join(f'Note: "{s}"' if i in hits else s for i, s in enumerate(sentences))
    return f"{story}\n\n{question}"


_TRANSFORMS = {"witness_restatement": witness_restatement, "witness_emphasis": witness_emphasis}


def apply_narrative_conditions(world: World, name_map: NameMap, rendered: RenderedTask, keys, seed: int = 0) -> str:
    """The prompt under the requested EF transform(s). Raises
    `EvidenceUnavailable` when the world has nothing to restate or flag."""
    keys = list(keys)
    if len(keys) != 1:
        raise EvidenceUnavailable(f"exactly one narrative EF transform at a time, got {keys}")
    return _TRANSFORMS[keys[0]](world, name_map, rendered, seed)
