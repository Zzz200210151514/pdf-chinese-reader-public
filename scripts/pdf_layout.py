"""Enforce the requested PDF reading form; geometry errors cannot authorize a change."""


def validate_layout(manifest, builder_mode):
    mode = manifest.get('layout_mode', 'source')
    requested = manifest.get('requested_layout', 'source')
    allowed = {'source', 'appendix'}
    if mode not in allowed or requested not in allowed:
        raise ValueError('layout_mode and requested_layout must be source or appendix')
    if mode != builder_mode:
        raise ValueError(f'This builder produces {builder_mode}; manifest requests {mode}')
    if mode != requested and manifest.get('layout_change_authorized') is not True:
        raise ValueError('PDF layout change is not authorized. Repair source-layout regions '
                         'first; a local collision or overflow does not authorize appendix pages.')
    return {'mode': mode, 'requested': requested,
            'change_authorized': manifest.get('layout_change_authorized') is True,
            'reference_layout_supplied': bool(manifest.get('reference_layout'))}
