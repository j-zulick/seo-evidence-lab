"""Advisory release differences; no executable registry writes."""
from .contracts import GuidanceVersion, RuleBinding, record_bytes


def compare_releases(repository, previous_id, current_id):
    previous, old = repository.graph(previous_id)
    current, new = repository.graph(current_id)
    old, new = ({r.id: r for r in graph if isinstance(r, (GuidanceVersion, RuleBinding))}
                for graph in (old, new))
    return dict(previous_release_id=previous.id, current_release_id=current.id,
                changes=[dict(id=key, before=old[key].model_dump(mode='json') if key in old else None,
                              after=new[key].model_dump(mode='json') if key in new else None)
                         for key in sorted(old.keys() | new.keys())
                         if key not in old or key not in new
                         or record_bytes(old[key]) != record_bytes(new[key])],
                executable_behavior_changed=False)
