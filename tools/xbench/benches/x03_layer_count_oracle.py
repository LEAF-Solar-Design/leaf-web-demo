"""Count model-space DXF INSERT instances of the requested panel block."""
def layer_count_quality(entities, layer, block, reported):
    instances = {}
    for entity in entities:
        if (entity.dxftype() == "INSERT" and entity.dxf.layer.casefold() == layer.casefold()
                and entity.dxf.name.casefold() == block.casefold()):
            handle = entity.dxf.get("handle")
            if not handle:
                return "INCOMPLETE"
            instances[handle.upper()] = entity.mcount
    return "PASS" if type(reported) is int and reported == sum(instances.values()) else "FAIL"
