import { describe, expect, it } from 'vitest'
import { buildSizingParams, sizingGraph, sizingTargets } from './solarSizingModel.js'

// The sizing form on a converted Ground design. A converted frame stores its panels as one compact slot block
// (frame.ground_slots, codec leaf.solar-ground-slots.v1) and graph.panels stays empty; the server's sizing rule
// (solar_sizing_client.sizing_targets) counts those slot panels. Every expected value in SERVER below is the
// server's own answer, measured by running its slot codec and sizing rule on these graphs and projected to the keys
// the form reads. Not representable here: a float count such as 3.0, which JSON.parse cannot tell from 3.
const SERVER = {
 "blocks": [
  {
   "block": {
    "codec": "leaf.solar-ground-slots.v1",
    "count": 3,
    "panel_ids": "pOJc3Wd1QD2eDCdUAGmTCGcLwrBGQEWOsS59L2ZKZ2alPO0HCrdNyo7ByPGYLh/I",
    "centres": 'A'.repeat(64),
    "angle": 0,
    "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
   },
   "ids": [
    "leaf:panel:a4e25cdd-6775-403d-9e0c-275400699308",
    "leaf:panel:670bc2b0-4640-458e-b12e-7d2f664a6766",
    "leaf:panel:a53ced07-0ab7-4dca-8ec1-c8f1982e1fc8"
   ],
   "label": "real",
   "ok": true
  },
  {
   "block": {
    "codec": "leaf.solar-ground-slots.v1",
    "count": 0,
    "panel_ids": "pOJc3Wd1QD2eDCdUAGmTCGcLwrBGQEWOsS59L2ZKZ2alPO0HCrdNyo7ByPGYLh/I",
    "centres": 'A'.repeat(64),
    "angle": 0,
    "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
   },
   "label": "count 0",
   "ok": false
  },
  {
   "block": {
    "codec": "leaf.solar-ground-slots.v1",
    "count": true,
    "panel_ids": "pOJc3Wd1QD2eDCdUAGmTCGcLwrBGQEWOsS59L2ZKZ2alPO0HCrdNyo7ByPGYLh/I",
    "centres": 'A'.repeat(64),
    "angle": 0,
    "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
   },
   "label": "count bool",
   "ok": false
  },
  {
   "block": {
    "codec": "leaf.solar-ground-slots.v1",
    "count": 4,
    "panel_ids": "pOJc3Wd1QD2eDCdUAGmTCGcLwrBGQEWOsS59L2ZKZ2alPO0HCrdNyo7ByPGYLh/I",
    "centres": 'A'.repeat(64),
    "angle": 0,
    "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
   },
   "label": "count one more",
   "ok": false
  },
  {
   "block": {
    "codec": "leaf.solar-ground-slots.v2",
    "count": 3,
    "panel_ids": "pOJc3Wd1QD2eDCdUAGmTCGcLwrBGQEWOsS59L2ZKZ2alPO0HCrdNyo7ByPGYLh/I",
    "centres": 'A'.repeat(64),
    "angle": 0,
    "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
   },
   "label": "codec other",
   "ok": false
  },
  {
   "block": {
    "codec": "leaf.solar-ground-slots.v1",
    "count": 3,
    "panel_ids": null,
    "centres": 'A'.repeat(64),
    "angle": 0,
    "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
   },
   "label": "panel_ids not text",
   "ok": false
  },
  {
   "block": {
    "codec": "leaf.solar-ground-slots.v1",
    "count": 3,
    "panel_ids": "pOJc3Wd1QD2eDCdUAGmTCGcLwrBGQEWOsS59L2ZKZ2alPO0HCrdNyo7ByPGYLh/I",
    "centres": 'A'.repeat(64),
    "angle": 0,
    "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
   },
   "ids": [
    "leaf:panel:a4e25cdd-6775-403d-9e0c-275400699308",
    "leaf:panel:670bc2b0-4640-458e-b12e-7d2f664a6766",
    "leaf:panel:a53ced07-0ab7-4dca-8ec1-c8f1982e1fc8"
   ],
   "label": "panel_ids unpadded",
   "ok": true
  },
  {
   "block": {
    "codec": "leaf.solar-ground-slots.v1",
    "count": 3,
    "panel_ids": "pOJc\n3Wd1QD2eDCdUAGmTCGcLwrBGQEWOsS59L2ZKZ2alPO0HCrdNyo7ByPGYLh/I",
    "centres": 'A'.repeat(64),
    "angle": 0,
    "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
   },
   "label": "panel_ids with newline",
   "ok": false
  },
  {
   "block": {
    "codec": "leaf.solar-ground-slots.v1",
    "count": 3,
    "panel_ids": "pOJc3Wd1QD2eDCdUAGmTCGcLwrBGQEWOsS59L2ZKZ2alPO0HCrdNyo7ByPGYLh_I",
    "centres": 'A'.repeat(64),
    "angle": 0,
    "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
   },
   "label": "panel_ids urlsafe alphabet",
   "ok": false
  },
  {
   "block": {
    "codec": "leaf.solar-ground-slots.v1",
    "count": 3,
    "panel_ids": "pOJc3Wd1ED2eDCdUAGmTCGcLwrBGQEWOsS59L2ZKZ2alPO0HCrdNyo7ByPGYLh/I",
    "centres": 'A'.repeat(64),
    "angle": 0,
    "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
   },
   "label": "uuid version 1",
   "ok": false
  },
  {
   "block": {
    "codec": "leaf.solar-ground-slots.v1",
    "count": 3,
    "panel_ids": "pOJc3Wd1QD3eDCdUAGmTCGcLwrBGQEWOsS59L2ZKZ2alPO0HCrdNyo7ByPGYLh/I",
    "centres": 'A'.repeat(64),
    "angle": 0,
    "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
   },
   "label": "uuid variant c",
   "ok": false
  },
  {
   "block": {
    "codec": "leaf.solar-ground-slots.v1",
    "count": 3,
    "panel_ids": "pOJc3Wd1QD2eDCdUAGmTCKTiXN1ndUA9ngwnVABpkwilPO0HCrdNyo7ByPGYLh/I",
    "centres": 'A'.repeat(64),
    "angle": 0,
    "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
   },
   "label": "duplicate id inside one block",
   "ok": false
  }
 ],
 "extra": [
  {
   "code": "DUPLICATE_APPLICATION_ID",
   "global": {
    "code": "DUPLICATE_APPLICATION_ID",
    "ok": false
   },
   "graph": {
    "electrical_zones": [],
    "frames": [
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 3,
       "panel_ids": "pOJc3Wd1QD2eDCdUAGmTCGcLwrBGQEWOsS59L2ZKZ2alPO0HCrdNyo7ByPGYLh/I",
       "centres": 'A'.repeat(64),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     },
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 3,
       "panel_ids": "pOJc3Wd1QD2eDCdUAGmTCGcLwrBGQEWOsS59L2ZKZ2alPO0HCrdNyo7ByPGYLh/I",
       "centres": 'A'.repeat(64),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     }
    ],
    "panels": [],
    "project": {
     "zip_code": "44224"
    },
    "rev": 1,
    "settings": {
     "extra": {},
     "id": "leaf:settings:00000000-0000-4000-8000-000000000001"
    }
   },
   "label": "two frames repeat one valid block",
   "zones": {
    "code": "DUPLICATE_APPLICATION_ID",
    "ok": false
   }
  },
  {
   "global": {
    "ok": true,
    "targets": [
     "leaf:settings:00000000-0000-4000-8000-000000000001"
    ]
   },
   "graph": {
    "electrical_zones": [],
    "frames": [
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 1,
       "panel_ids": "pOJc3Wd1QD2eDCdUAGmTCA==",
       "centres": btoa('\0'.repeat(1 * 16)),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     },
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 2,
       "panel_ids": "qJ+t4fMxRXidcTMkFzN7mc5D4m6mYUILgk3eP/egvgE=",
       "centres": btoa('\0'.repeat(2 * 16)),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     }
    ],
    "panels": [],
    "project": {
     "zip_code": "44224"
    },
    "rev": 1,
    "settings": {
     "extra": {},
     "id": "leaf:settings:00000000-0000-4000-8000-000000000001"
    }
   },
   "label": "a one-slot block beside a two-slot block",
   "slot_ids": [
    "leaf:panel:a4e25cdd-6775-403d-9e0c-275400699308",
    "leaf:panel:a89fade1-f331-4578-9d71-332417337b99",
    "leaf:panel:ce43e26e-a661-420b-824d-de3ff7a0be01"
   ],
   "zones": {
    "code": "INVALID_ZONE_COVERAGE",
    "ok": false
   }
  },
  {
   "global": {
    "ok": true,
    "targets": [
     "leaf:settings:00000000-0000-4000-8000-000000000001"
    ]
   },
   "graph": {
    "electrical_zones": [],
    "frames": [
     {},
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 2,
       "panel_ids": "qJ+t4fMxRXidcTMkFzN7mc5D4m6mYUILgk3eP/egvgE=",
       "centres": btoa('\0'.repeat(2 * 16)),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     }
    ],
    "panels": [],
    "project": {
     "zip_code": "44224"
    },
    "rev": 1,
    "settings": {
     "extra": {},
     "id": "leaf:settings:00000000-0000-4000-8000-000000000001"
    }
   },
   "label": "a frame without a block before a frame with one",
   "slot_ids": [
    "leaf:panel:a89fade1-f331-4578-9d71-332417337b99",
    "leaf:panel:ce43e26e-a661-420b-824d-de3ff7a0be01"
   ],
   "zones": {
    "code": "INVALID_ZONE_COVERAGE",
    "ok": false
   }
  },
  {
   "global": {
    "ok": true,
    "targets": [
     "leaf:settings:00000000-0000-4000-8000-000000000001"
    ]
   },
   "graph": {
    "electrical_zones": [],
    "frames": [
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 1,
       "panel_ids": "pOJc3Wd1QD2eDCdUAGmTCA==",
       "centres": btoa('\0'.repeat(1 * 16)),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     },
     {}
    ],
    "panels": [],
    "project": {
     "zip_code": "44224"
    },
    "rev": 1,
    "settings": {
     "extra": {},
     "id": "leaf:settings:00000000-0000-4000-8000-000000000001"
    }
   },
   "label": "one slot in the whole design",
   "slot_ids": [
    "leaf:panel:a4e25cdd-6775-403d-9e0c-275400699308"
   ],
   "zones": {
    "code": "INVALID_ZONE_COVERAGE",
    "ok": false
   }
  }
 ],
 "graphs": [
  {
   "global": {
    "ok": true,
    "targets": [
     "leaf:settings:00000000-0000-4000-8000-000000000001"
    ]
   },
   "graph": {
    "electrical_zones": [],
    "frames": [
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 3,
       "panel_ids": "pOJc3Wd1QD2eDCdUAGmTCGcLwrBGQEWOsS59L2ZKZ2alPO0HCrdNyo7ByPGYLh/I",
       "centres": 'A'.repeat(64),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     },
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 2,
       "panel_ids": "qJ+t4fMxRXidcTMkFzN7mc5D4m6mYUILgk3eP/egvgE=",
       "centres": btoa('\0'.repeat(2 * 16)),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     }
    ],
    "panels": [],
    "project": {
     "zip_code": "44224"
    },
    "rev": 1,
    "settings": {
     "extra": {},
     "id": "leaf:settings:00000000-0000-4000-8000-000000000001"
    }
   },
   "label": "converted",
   "slot_ids": [
    "leaf:panel:a4e25cdd-6775-403d-9e0c-275400699308",
    "leaf:panel:670bc2b0-4640-458e-b12e-7d2f664a6766",
    "leaf:panel:a53ced07-0ab7-4dca-8ec1-c8f1982e1fc8",
    "leaf:panel:a89fade1-f331-4578-9d71-332417337b99",
    "leaf:panel:ce43e26e-a661-420b-824d-de3ff7a0be01"
   ],
   "zones": {
    "code": "INVALID_ZONE_COVERAGE",
    "ok": false
   }
  },
  {
   "global": {
    "ok": true,
    "targets": [
     "leaf:settings:00000000-0000-4000-8000-000000000001"
    ]
   },
   "graph": {
    "electrical_zones": [],
    "frames": [
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 3,
       "panel_ids": "pOJc3Wd1QD2eDCdUAGmTCGcLwrBGQEWOsS59L2ZKZ2alPO0HCrdNyo7ByPGYLh/I",
       "centres": 'A'.repeat(64),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     },
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 2,
       "panel_ids": "qJ+t4fMxRXidcTMkFzN7mc5D4m6mYUILgk3eP/egvgE=",
       "centres": btoa('\0'.repeat(2 * 16)),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     }
    ],
    "panels": [],
    "project": {
     "zip_code": "44224"
    },
    "rev": 2,
    "settings": {
     "extra": {},
     "id": "leaf:settings:00000000-0000-4000-8000-000000000001"
    }
   },
   "label": "converted+sized",
   "slot_ids": [
    "leaf:panel:a4e25cdd-6775-403d-9e0c-275400699308",
    "leaf:panel:670bc2b0-4640-458e-b12e-7d2f664a6766",
    "leaf:panel:a53ced07-0ab7-4dca-8ec1-c8f1982e1fc8",
    "leaf:panel:a89fade1-f331-4578-9d71-332417337b99",
    "leaf:panel:ce43e26e-a661-420b-824d-de3ff7a0be01"
   ],
   "zones": {
    "code": "INVALID_ZONE_COVERAGE",
    "ok": false
   }
  },
  {
   "global": {
    "ok": true,
    "targets": [
     "leaf:settings:00000000-0000-4000-8000-000000000001"
    ]
   },
   "graph": {
    "electrical_zones": [],
    "frames": [
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 3,
       "panel_ids": "pOJc3Wd1QD2eDCdUAGmTCGcLwrBGQEWOsS59L2ZKZ2alPO0HCrdNyo7ByPGYLh/I",
       "centres": 'A'.repeat(64),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     },
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 2,
       "panel_ids": "qJ+t4fMxRXidcTMkFzN7mc5D4m6mYUILgk3eP/egvgE=",
       "centres": btoa('\0'.repeat(2 * 16)),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     }
    ],
    "panels": [],
    "project": {
     "zip_code": "44224"
    },
    "rev": 1,
    "settings": {
     "extra": {},
     "id": "leaf:settings:00000000-0000-4000-8000-000000000001"
    }
   },
   "label": "converted, no zones (zones mode)",
   "slot_ids": [
    "leaf:panel:a4e25cdd-6775-403d-9e0c-275400699308",
    "leaf:panel:670bc2b0-4640-458e-b12e-7d2f664a6766",
    "leaf:panel:a53ced07-0ab7-4dca-8ec1-c8f1982e1fc8",
    "leaf:panel:a89fade1-f331-4578-9d71-332417337b99",
    "leaf:panel:ce43e26e-a661-420b-824d-de3ff7a0be01"
   ],
   "zones": {
    "code": "INVALID_ZONE_COVERAGE",
    "ok": false
   }
  },
  {
   "global": {
    "code": "INVALID_GROUND_SLOTS",
    "ok": false
   },
   "graph": {
    "electrical_zones": [],
    "frames": [
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 3,
       "panel_ids": "pOJc3Wd1QD2eDCdUAGmTCGcLwrBGQEWOsS59L2ZKZ2alPO0HCrdNyo7ByPGYLh/I",
       "centres": 'A'.repeat(64),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     },
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 2,
       "panel_ids": "pOJc3Wd1QD2eDCdUAGmTCGcLwrBGQEWOsS59L2ZKZ2alPO0HCrdNyo7ByPGYLh/I",
       "centres": btoa('\0'.repeat(2 * 16)),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     }
    ],
    "panels": [],
    "project": {
     "zip_code": "44224"
    },
    "rev": 1,
    "settings": {
     "extra": {},
     "id": "leaf:settings:00000000-0000-4000-8000-000000000001"
    }
   },
   "label": "a block whose count disagrees with its payload",
   "zones": {
    "code": "INVALID_GROUND_SLOTS",
    "ok": false
   }
  }
 ],
 "zones": [
  {
   "global": {
    "ok": true,
    "targets": [
     "leaf:settings:00000000-0000-4000-8000-000000000001"
    ]
   },
   "graph": {
    "electrical_zones": [
     {
      "id": "ZA",
      "inverter_model_a": "Inverter ZA",
      "module_model": "Module ZA",
      "panel_refs": [
       "leaf:panel:a4e25cdd-6775-403d-9e0c-275400699308",
       "leaf:panel:670bc2b0-4640-458e-b12e-7d2f664a6766"
      ]
     },
     {
      "id": "ZB",
      "inverter_model_a": "Inverter ZB",
      "module_model": "Module ZB",
      "panel_refs": [
       "leaf:panel:a53ced07-0ab7-4dca-8ec1-c8f1982e1fc8",
       "leaf:panel:a89fade1-f331-4578-9d71-332417337b99",
       "leaf:panel:ce43e26e-a661-420b-824d-de3ff7a0be01"
      ]
     }
    ],
    "frames": [
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 3,
       "panel_ids": "pOJc3Wd1QD2eDCdUAGmTCGcLwrBGQEWOsS59L2ZKZ2alPO0HCrdNyo7ByPGYLh/I",
       "centres": 'A'.repeat(64),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     },
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 2,
       "panel_ids": "qJ+t4fMxRXidcTMkFzN7mc5D4m6mYUILgk3eP/egvgE=",
       "centres": btoa('\0'.repeat(2 * 16)),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     }
    ],
    "panels": [],
    "project": {
     "zip_code": "44224"
    },
    "rev": 1,
    "settings": {
     "extra": {},
     "id": "leaf:settings:00000000-0000-4000-8000-000000000001"
    }
   },
   "label": "two zones cover every slot once",
   "zones": {
    "ok": true,
    "targets": [
     "ZA",
     "ZB"
    ]
   }
  },
  {
   "global": {
    "ok": true,
    "targets": [
     "leaf:settings:00000000-0000-4000-8000-000000000001"
    ]
   },
   "graph": {
    "electrical_zones": [
     {
      "id": "ZA",
      "inverter_model_a": "Inverter ZA",
      "module_model": "Module ZA",
      "panel_refs": [
       "leaf:panel:a4e25cdd-6775-403d-9e0c-275400699308",
       "leaf:panel:670bc2b0-4640-458e-b12e-7d2f664a6766",
       "leaf:panel:a53ced07-0ab7-4dca-8ec1-c8f1982e1fc8",
       "leaf:panel:a89fade1-f331-4578-9d71-332417337b99",
       "leaf:panel:ce43e26e-a661-420b-824d-de3ff7a0be01"
      ]
     }
    ],
    "frames": [
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 3,
       "panel_ids": "pOJc3Wd1QD2eDCdUAGmTCGcLwrBGQEWOsS59L2ZKZ2alPO0HCrdNyo7ByPGYLh/I",
       "centres": 'A'.repeat(64),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     },
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 2,
       "panel_ids": "qJ+t4fMxRXidcTMkFzN7mc5D4m6mYUILgk3eP/egvgE=",
       "centres": btoa('\0'.repeat(2 * 16)),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     }
    ],
    "panels": [],
    "project": {
     "zip_code": "44224"
    },
    "rev": 1,
    "settings": {
     "extra": {},
     "id": "leaf:settings:00000000-0000-4000-8000-000000000001"
    }
   },
   "label": "one zone covers every slot",
   "zones": {
    "ok": true,
    "targets": [
     "ZA"
    ]
   }
  },
  {
   "global": {
    "ok": true,
    "targets": [
     "leaf:settings:00000000-0000-4000-8000-000000000001"
    ]
   },
   "graph": {
    "electrical_zones": [
     {
      "id": "ZA",
      "inverter_model_a": "Inverter ZA",
      "module_model": "Module ZA",
      "panel_refs": [
       "leaf:panel:a4e25cdd-6775-403d-9e0c-275400699308",
       "leaf:panel:670bc2b0-4640-458e-b12e-7d2f664a6766",
       "leaf:panel:a53ced07-0ab7-4dca-8ec1-c8f1982e1fc8",
       "leaf:panel:a89fade1-f331-4578-9d71-332417337b99"
      ]
     }
    ],
    "frames": [
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 3,
       "panel_ids": "pOJc3Wd1QD2eDCdUAGmTCGcLwrBGQEWOsS59L2ZKZ2alPO0HCrdNyo7ByPGYLh/I",
       "centres": 'A'.repeat(64),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     },
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 2,
       "panel_ids": "qJ+t4fMxRXidcTMkFzN7mc5D4m6mYUILgk3eP/egvgE=",
       "centres": btoa('\0'.repeat(2 * 16)),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     }
    ],
    "panels": [],
    "project": {
     "zip_code": "44224"
    },
    "rev": 1,
    "settings": {
     "extra": {},
     "id": "leaf:settings:00000000-0000-4000-8000-000000000001"
    }
   },
   "label": "one slot uncovered",
   "zones": {
    "code": "INVALID_ZONE_COVERAGE",
    "ok": false
   }
  },
  {
   "global": {
    "ok": true,
    "targets": [
     "leaf:settings:00000000-0000-4000-8000-000000000001"
    ]
   },
   "graph": {
    "electrical_zones": [
     {
      "id": "ZA",
      "inverter_model_a": "Inverter ZA",
      "module_model": "Module ZA",
      "panel_refs": [
       "leaf:panel:a4e25cdd-6775-403d-9e0c-275400699308",
       "leaf:panel:670bc2b0-4640-458e-b12e-7d2f664a6766",
       "leaf:panel:a53ced07-0ab7-4dca-8ec1-c8f1982e1fc8",
       "leaf:panel:a89fade1-f331-4578-9d71-332417337b99",
       "leaf:panel:ce43e26e-a661-420b-824d-de3ff7a0be01"
      ]
     },
     {
      "id": "ZB",
      "inverter_model_a": "Inverter ZB",
      "module_model": "Module ZB",
      "panel_refs": [
       "leaf:panel:a4e25cdd-6775-403d-9e0c-275400699308"
      ]
     }
    ],
    "frames": [
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 3,
       "panel_ids": "pOJc3Wd1QD2eDCdUAGmTCGcLwrBGQEWOsS59L2ZKZ2alPO0HCrdNyo7ByPGYLh/I",
       "centres": 'A'.repeat(64),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     },
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 2,
       "panel_ids": "qJ+t4fMxRXidcTMkFzN7mc5D4m6mYUILgk3eP/egvgE=",
       "centres": btoa('\0'.repeat(2 * 16)),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     }
    ],
    "panels": [],
    "project": {
     "zip_code": "44224"
    },
    "rev": 1,
    "settings": {
     "extra": {},
     "id": "leaf:settings:00000000-0000-4000-8000-000000000001"
    }
   },
   "label": "a slot covered twice",
   "zones": {
    "code": "INVALID_ZONE_COVERAGE",
    "ok": false
   }
  },
  {
   "global": {
    "ok": true,
    "targets": [
     "leaf:settings:00000000-0000-4000-8000-000000000001"
    ]
   },
   "graph": {
    "electrical_zones": [
     {
      "id": "ZA",
      "inverter_model_a": "Inverter ZA",
      "module_model": "Module ZA",
      "panel_refs": [
       "leaf:panel:a4e25cdd-6775-403d-9e0c-275400699308",
       "leaf:panel:670bc2b0-4640-458e-b12e-7d2f664a6766",
       "leaf:panel:a53ced07-0ab7-4dca-8ec1-c8f1982e1fc8",
       "leaf:panel:a89fade1-f331-4578-9d71-332417337b99",
       "leaf:panel:ce43e26e-a661-420b-824d-de3ff7a0be01",
       "leaf:panel:22222222-2222-4222-8222-222222222222"
      ]
     }
    ],
    "frames": [
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 3,
       "panel_ids": "pOJc3Wd1QD2eDCdUAGmTCGcLwrBGQEWOsS59L2ZKZ2alPO0HCrdNyo7ByPGYLh/I",
       "centres": 'A'.repeat(64),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     },
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 2,
       "panel_ids": "qJ+t4fMxRXidcTMkFzN7mc5D4m6mYUILgk3eP/egvgE=",
       "centres": btoa('\0'.repeat(2 * 16)),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     }
    ],
    "panels": [],
    "project": {
     "zip_code": "44224"
    },
    "rev": 1,
    "settings": {
     "extra": {},
     "id": "leaf:settings:00000000-0000-4000-8000-000000000001"
    }
   },
   "label": "a zone names an unknown panel",
   "zones": {
    "code": "INVALID_ZONE_COVERAGE",
    "ok": false
   }
  },
  {
   "global": {
    "ok": true,
    "targets": [
     "leaf:settings:00000000-0000-4000-8000-000000000001"
    ]
   },
   "graph": {
    "electrical_zones": [
     {
      "id": "ZA",
      "inverter_model_a": "Inverter ZA",
      "module_model": "Module ZA",
      "panel_refs": [
       "leaf:panel:a4e25cdd-6775-403d-9e0c-275400699308",
       "leaf:panel:670bc2b0-4640-458e-b12e-7d2f664a6766",
       "leaf:panel:a53ced07-0ab7-4dca-8ec1-c8f1982e1fc8",
       "leaf:panel:a89fade1-f331-4578-9d71-332417337b99",
       "leaf:panel:ce43e26e-a661-420b-824d-de3ff7a0be01"
      ]
     },
     {
      "id": "ZB",
      "inverter_model_a": "Inverter ZB",
      "module_model": "Module ZB",
      "panel_refs": []
     }
    ],
    "frames": [
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 3,
       "panel_ids": "pOJc3Wd1QD2eDCdUAGmTCGcLwrBGQEWOsS59L2ZKZ2alPO0HCrdNyo7ByPGYLh/I",
       "centres": 'A'.repeat(64),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     },
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 2,
       "panel_ids": "qJ+t4fMxRXidcTMkFzN7mc5D4m6mYUILgk3eP/egvgE=",
       "centres": btoa('\0'.repeat(2 * 16)),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     }
    ],
    "panels": [],
    "project": {
     "zip_code": "44224"
    },
    "rev": 1,
    "settings": {
     "extra": {},
     "id": "leaf:settings:00000000-0000-4000-8000-000000000001"
    }
   },
   "label": "an empty zone",
   "zones": {
    "code": "INVALID_ZONE_COVERAGE",
    "ok": false
   }
  },
  {
   "global": {
    "ok": true,
    "targets": [
     "leaf:settings:00000000-0000-4000-8000-000000000001"
    ]
   },
   "graph": {
    "electrical_zones": [
     {
      "id": "ZA",
      "inverter_model_a": "Inverter ZA",
      "module_model": "Module ZA",
      "panel_refs": [
       "leaf:panel:11111111-1111-4111-8111-111111111111",
       "leaf:panel:a4e25cdd-6775-403d-9e0c-275400699308",
       "leaf:panel:670bc2b0-4640-458e-b12e-7d2f664a6766",
       "leaf:panel:a53ced07-0ab7-4dca-8ec1-c8f1982e1fc8",
       "leaf:panel:a89fade1-f331-4578-9d71-332417337b99",
       "leaf:panel:ce43e26e-a661-420b-824d-de3ff7a0be01"
      ]
     }
    ],
    "frames": [
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 3,
       "panel_ids": "pOJc3Wd1QD2eDCdUAGmTCGcLwrBGQEWOsS59L2ZKZ2alPO0HCrdNyo7ByPGYLh/I",
       "centres": 'A'.repeat(64),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     },
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 2,
       "panel_ids": "qJ+t4fMxRXidcTMkFzN7mc5D4m6mYUILgk3eP/egvgE=",
       "centres": btoa('\0'.repeat(2 * 16)),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     }
    ],
    "panels": [
     {
      "id": "leaf:panel:11111111-1111-4111-8111-111111111111"
     }
    ],
    "project": {
     "zip_code": "44224"
    },
    "rev": 1,
    "settings": {
     "extra": {},
     "id": "leaf:settings:00000000-0000-4000-8000-000000000001"
    }
   },
   "label": "row panel and slots covered together",
   "zones": {
    "ok": true,
    "targets": [
     "ZA"
    ]
   }
  },
  {
   "global": {
    "ok": true,
    "targets": [
     "leaf:settings:00000000-0000-4000-8000-000000000001"
    ]
   },
   "graph": {
    "electrical_zones": [
     {
      "id": "ZA",
      "inverter_model_a": "Inverter ZA",
      "module_model": "Module ZA",
      "panel_refs": [
       "leaf:panel:a4e25cdd-6775-403d-9e0c-275400699308",
       "leaf:panel:670bc2b0-4640-458e-b12e-7d2f664a6766",
       "leaf:panel:a53ced07-0ab7-4dca-8ec1-c8f1982e1fc8",
       "leaf:panel:a89fade1-f331-4578-9d71-332417337b99",
       "leaf:panel:ce43e26e-a661-420b-824d-de3ff7a0be01"
      ]
     }
    ],
    "frames": [
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 3,
       "panel_ids": "pOJc3Wd1QD2eDCdUAGmTCGcLwrBGQEWOsS59L2ZKZ2alPO0HCrdNyo7ByPGYLh/I",
       "centres": 'A'.repeat(64),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     },
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 2,
       "panel_ids": "qJ+t4fMxRXidcTMkFzN7mc5D4m6mYUILgk3eP/egvgE=",
       "centres": btoa('\0'.repeat(2 * 16)),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     }
    ],
    "panels": [
     {
      "id": "leaf:panel:11111111-1111-4111-8111-111111111111"
     }
    ],
    "project": {
     "zip_code": "44224"
    },
    "rev": 1,
    "settings": {
     "extra": {},
     "id": "leaf:settings:00000000-0000-4000-8000-000000000001"
    }
   },
   "label": "row panel uncovered beside slots",
   "zones": {
    "code": "INVALID_ZONE_COVERAGE",
    "ok": false
   }
  },
  {
   "global": {
    "ok": true,
    "targets": [
     "leaf:settings:00000000-0000-4000-8000-000000000001"
    ]
   },
   "graph": {
    "electrical_zones": [
     {
      "id": "ZA",
      "inverter_model_a": "Inverter ZA",
      "module_model": "Module ZA",
      "panel_refs": [
       "leaf:panel:a4e25cdd-6775-403d-9e0c-275400699308",
       "leaf:panel:670bc2b0-4640-458e-b12e-7d2f664a6766",
       "leaf:panel:a53ced07-0ab7-4dca-8ec1-c8f1982e1fc8",
       "leaf:panel:a89fade1-f331-4578-9d71-332417337b99",
       "leaf:panel:ce43e26e-a661-420b-824d-de3ff7a0be01"
      ]
     }
    ],
    "frames": [
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 3,
       "panel_ids": "pOJc3Wd1QD2eDCdUAGmTCGcLwrBGQEWOsS59L2ZKZ2alPO0HCrdNyo7ByPGYLh/I",
       "centres": 'A'.repeat(64),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     },
     {
      "ground_slots": {
       "codec": "leaf.solar-ground-slots.v1",
       "count": 2,
       "panel_ids": "qJ+t4fMxRXidcTMkFzN7mc5D4m6mYUILgk3eP/egvgE=",
       "centres": btoa('\0'.repeat(2 * 16)),
       "angle": 0,
       "panel": { rev: 0, provenance: {}, validity: {}, extra: {} }
      }
     }
    ],
    "panels": [
     {
      "id": "leaf:panel:a4e25cdd-6775-403d-9e0c-275400699308"
     }
    ],
    "project": {
     "zip_code": "44224"
    },
    "rev": 1,
    "settings": {
     "extra": {},
     "id": "leaf:settings:00000000-0000-4000-8000-000000000001"
    }
   },
   "label": "row panel repeats a slot id",
   "zones": {
    "ok": true,
    "targets": [
     "ZA"
    ]
   }
  }
 ]
}

const CODE = { INVALID_ZONE_COVERAGE: 'sizing_zones_invalid', MISSING_PANEL: 'sizing_panels_required' }
const UNAVAILABLE = { ok: false, reason: 'sizing_graph_unavailable' }
const read = (graph) => sizingGraph({ version: 3, intake: { solar_design_graph: graph } }, 3)
const converted = () => structuredClone(SERVER.graphs.find((item) => item.label === 'converted').graph)
const plainGraph = () => ({ rev: 7, settings: { id: 'S', extra: {} }, project: { zip_code: '44224' },
  panels: [{ id: 'P1' }], electrical_zones: [] })

// The form's answer for one mode, in the server's vocabulary.
const answer = (graph, mode) => {
  const view = read(graph)
  if (!view.ok) return { ok: false, reason: view.reason }
  const got = sizingTargets(view, mode)
  return got.ok ? { ok: true, targets: got.targets } : { ok: false, reason: got.reason }
}
const expected = (server) => (server.ok ? { ok: true, targets: server.targets }
  : { ok: false, reason: CODE[server.code] ?? 'sizing_graph_unavailable' })

// count slots, each a distinct UUIDv4 (version nibble 4, variant 8), in the codec's canonical padded base64.
const slotBlock = (count, seed = 0) => {
  let bytes = ''
  for (let index = 0; index < count; index += 1) {
    const n = seed * 1000003 + index
    bytes += String.fromCharCode((n >>> 24) & 255, (n >>> 16) & 255, (n >>> 8) & 255, n & 255,
      0, 0, 0x40, 0, 0x80, 0, 0, 0, 0, 0, seed & 255, 1)
  }
  return { codec: 'leaf.solar-ground-slots.v1', count, panel_ids: btoa(bytes),
    centres: btoa('\0'.repeat(count * 16)), angle: 0,
    panel: { rev: 0, provenance: {}, validity: {}, extra: {} } }
}

describe('solarSizingModel on a converted Ground design', () => {
  it('GSZ1 reads the slot panel ids the server decodes, frames in graph order then slot order', () => {
    const rows = SERVER.graphs.filter((item) => item.slot_ids)
    expect(rows.length).toBeGreaterThanOrEqual(2)
    for (const item of rows) {
      const view = read(item.graph)
      expect(view.ok, item.label).toBe(true)
      expect(view.slotIds, item.label).toEqual(item.slot_ids)
      expect(view.panels, item.label).toEqual([])
    }
  })

  it.each(SERVER.graphs.map((item) => [item.label, item]))('GSZ2 sizing targets agree with the server: %s', (_label, item) => {
    expect(answer(item.graph, 'global')).toEqual(expected(item.global))
    expect(answer(item.graph, 'zones')).toEqual(expected(item.zones))
  })

  it.each(SERVER.zones.map((item) => [item.label, item]))('GSZ3 zone coverage over slot panels agrees with the server: %s', (_label, item) => {
    expect(answer(item.graph, 'global')).toEqual(expected(item.global))
    expect(answer(item.graph, 'zones')).toEqual(expected(item.zones))
  })

  it.each(SERVER.blocks.map((item) => [item.label, item]))('GSZ4 a slot block the server refuses makes the form unavailable: %s', (_label, item) => {
    const graph = converted()
    graph.frames = [{ ground_slots: item.block }]
    const view = read(graph)
    if (item.ok) {
      expect(view.ok).toBe(true)
      expect(view.slotIds).toEqual(item.ids)
    } else {
      expect(view).toEqual(UNAVAILABLE)
    }
  })

  it('GSZ5 refuses a block that is not a plain object, a non-integer count and frames that are not a list', () => {
    for (const block of [null, [], 'x', 7, { ...slotBlock(1), count: 1.5 }]) {
      const graph = converted()
      graph.frames = [{ ground_slots: block }]
      expect(read(graph), JSON.stringify(block)).toEqual(UNAVAILABLE)
    }
    for (const frames of [{}, 'x', 7, null]) expect(read({ ...converted(), frames }), JSON.stringify(frames)).toEqual(UNAVAILABLE)
  })

  it('GSZ6 leaves a design without slot blocks exactly as before', () => {
    expect(read(plainGraph()).slotIds).toEqual([])
    expect(read({ ...plainGraph(), frames: [] }).slotIds).toEqual([])
    expect(read({ ...plainGraph(), frames: [{ id: 'F' }, {}] }).slotIds).toEqual([])
    expect(answer(plainGraph(), 'global')).toEqual({ ok: true, targets: ['S'] })
    expect(answer({ ...plainGraph(), panels: [] }, 'global')).toEqual({ ok: false, reason: 'sizing_panels_required' })
    expect(answer({ ...plainGraph(), panels: [], frames: [{ id: 'F' }] }, 'global'))
      .toEqual({ ok: false, reason: 'sizing_panels_required' })
  })

  it('GSZ7 holds the server bounds: 10000 slots a frame and 100000 a graph', () => {
    const one = (count) => read({ ...converted(), frames: [{ ground_slots: slotBlock(count) }] })
    expect(one(10000).slotIds).toHaveLength(10000)
    expect(one(10001)).toEqual(UNAVAILABLE)
    const frames = Array.from({ length: 10 }, (_unused, seed) => ({ ground_slots: slotBlock(10000, seed) }))
    expect(read({ ...converted(), frames }).slotIds).toHaveLength(100000)
    expect(read({ ...converted(), frames: [...frames, { ground_slots: slotBlock(1, 10) }] })).toEqual(UNAVAILABLE)
  })

  it.each(SERVER.extra.map((item) => [item.label, item]))('GSZ9 slot ids across frames agree with the server: %s', (_label, item) => {
    const view = read(item.graph)
    if (item.slot_ids) {
      expect(view.ok).toBe(true)
      expect(view.slotIds).toEqual(item.slot_ids)
    } else {
      // The server refuses with item.code; a repeated id must never become a shorter panel set.
      expect(item.code).toBe('DUPLICATE_APPLICATION_ID')
      expect(view).toEqual(UNAVAILABLE)
    }
    expect(answer(item.graph, 'global')).toEqual(expected(item.global))
    expect(answer(item.graph, 'zones')).toEqual(expected(item.zones))
  })

  it('GSZ8 builds a global sizing request for a converted design and mutates nothing', () => {
    const graph = converted()
    graph.project.zip_code = '44224'
    const before = structuredClone(graph)
    const view = read(graph)
    const built = buildSizingParams({ graph: view, mode: 'global', draft: {
      module_name: 'Module', full_inverter_name: 'Inverter', bifacial: false, bifacial_coefficient: '.7',
      racking_type: 'fixed_tilt', surface_tilt: '5', surface_azimuth: '180', albedo: '.25', max_voltage: '1500',
      thermal_model_type: 'close mount glass glass', open_circuit_rise: false, grant_ref: 'grant_1' } })
    expect(built.ok).toBe(true)
    expect(graph).toEqual(before)
    expect(read(graph).slotIds).toEqual(view.slotIds)
  })
})
