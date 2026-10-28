"""Resolve a role selection once, then split it into balanced, disjoint shards."""


def should_split_states(role, location, capped, *, mode='auto', terms=None):
    """All roles can expand in auto mode; explicit all overrides legacy allowlists."""
    if mode not in {'auto', 'all'}:
        raise ValueError('State split mode must be auto or all')
    if location.strip().casefold() not in {'united states', 'usa', 'us', 'united states of america'}:
        return False
    if mode == 'all':
        return True
    allowed = terms is None or role.casefold() in {term.casefold() for term in terms}
    return allowed and (capped or terms is not None)


def select_roles(roles, *, filters=None, start_idx=1, end_idx=None, limit=None, shard=None):
    selected = list(dict.fromkeys(roles))
    # Workflow ranges own disjoint sections of the catalog. Filters narrow that
    # range rather than pulling similarly named roles from another workflow.
    selected = selected[max(0, (start_idx or 1) - 1):end_idx]
    if filters is not None:
        terms = list(dict.fromkeys(term.strip().casefold() for term in filters if term.strip()))
        if not terms:
            raise ValueError('Provide at least one role filter')
        unknown = [term for term in terms if not any(term in role.casefold() for role in selected)]
        if unknown:
            raise ValueError('No role matching: ' + ', '.join(unknown))
        # Overlapping filters select a role once, preserving catalog order.
        selected = [role for role in selected if any(term in role.casefold() for term in terms)]
    if limit:
        selected = selected[:limit]
    if shard:
        try:
            part, total = map(int, shard.split('/'))
        except (ValueError, AttributeError):
            raise ValueError('Shard must be part/total, for example 1/8') from None
        if not 1 <= part <= total:
            raise ValueError('Shard must satisfy 1 <= part <= total')
        size, remainder = divmod(len(selected), total)
        start = (part - 1) * size + min(part - 1, remainder)
        end = start + size + (part <= remainder)
        selected = selected[start:end]
    return selected
