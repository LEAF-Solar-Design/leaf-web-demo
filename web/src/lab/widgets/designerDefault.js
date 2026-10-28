// Frozen migrate() snapshot of contract/fixtures/widget/designer-v2-default.json.
// Keep browser imports within web/; designer.test.jsx pins this to the fixture.
// Seed the explicit store-backed New action; local drafts start empty.
function freeze(value) {
  if (value && typeof value === 'object') {
    Object.values(value).forEach(freeze)
    Object.freeze(value)
  }
  return value
}
export const DESIGNER_DEFAULT_DOC = freeze({
  "schema": "widget-doc.v1",
  "min_renderer": "1.0.0",
  "active": "la",
  "formats": {
    "la": {
      "h": 128,
      "radius": 22,
      "bg": "black",
      "vibrant": false,
      "ship": true,
      "els": [
        {
          "id": "brand",
          "x": 14,
          "y": 10,
          "w": 132,
          "h": 18,
          "scale": 1,
          "weight": 800,
          "color": "leaf",
          "align": "left",
          "visible": true,
          "tracking": 3,
          "kind": "text",
          "preset": "brand",
          "min_renderer": "1.0.0"
        },
        {
          "id": "total",
          "x": 227,
          "y": 9,
          "w": 120,
          "h": 20,
          "scale": 1,
          "weight": 600,
          "color": "ink",
          "align": "right",
          "visible": true,
          "kind": "text",
          "preset": "total",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "total",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "ink"
          }
        },
        {
          "id": "symbols",
          "x": 14,
          "y": 34,
          "w": 150,
          "h": 84,
          "scale": 1,
          "weight": 600,
          "color": "ink",
          "align": "left",
          "visible": true,
          "maxRows": 3,
          "kind": "text",
          "preset": "symbols",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "symbols",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "ink"
          }
        },
        {
          "id": "pnl",
          "x": 166,
          "y": 34,
          "w": 181,
          "h": 84,
          "scale": 1,
          "weight": 700,
          "color": "auto",
          "align": "right",
          "visible": true,
          "arrows": true,
          "maxRows": 3,
          "kind": "text",
          "preset": "pnl",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "pnl",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "auto"
          }
        }
      ]
    },
    "diExp": {
      "h": 160,
      "radius": 44,
      "bg": "black",
      "vibrant": false,
      "ship": true,
      "els": [
        {
          "id": "leaf",
          "x": 22,
          "y": 10,
          "w": 22,
          "h": 22,
          "scale": 1,
          "weight": 600,
          "color": "leaf",
          "align": "left",
          "visible": true,
          "kind": "svg-path",
          "preset": "leafIcon",
          "min_renderer": "1.0.0"
        },
        {
          "id": "total",
          "x": 48,
          "y": 11,
          "w": 70,
          "h": 20,
          "scale": 0.9,
          "weight": 600,
          "color": "ink",
          "align": "left",
          "visible": true,
          "kind": "text",
          "preset": "total",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "total",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "ink"
          }
        },
        {
          "id": "openPnl",
          "x": 256,
          "y": 10,
          "w": 94,
          "h": 22,
          "scale": 1,
          "weight": 700,
          "color": "auto",
          "align": "right",
          "visible": true,
          "arrows": true,
          "kind": "text",
          "preset": "openPnlSum",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "openPnlSum",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "auto"
          }
        },
        {
          "id": "symbols",
          "x": 22,
          "y": 46,
          "w": 150,
          "h": 70,
          "scale": 1,
          "weight": 600,
          "color": "ink",
          "align": "left",
          "visible": true,
          "maxRows": 3,
          "kind": "text",
          "preset": "symbols",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "symbols",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "ink"
          }
        },
        {
          "id": "pnl",
          "x": 176,
          "y": 46,
          "w": 174,
          "h": 70,
          "scale": 1,
          "weight": 700,
          "color": "auto",
          "align": "right",
          "visible": true,
          "arrows": true,
          "maxRows": 3,
          "kind": "text",
          "preset": "pnl",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "pnl",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "auto"
          }
        },
        {
          "id": "history",
          "x": 22,
          "y": 122,
          "w": 327,
          "h": 32,
          "scale": 1,
          "weight": 500,
          "color": "auto",
          "align": "left",
          "visible": true,
          "count": 4,
          "kind": "text",
          "preset": "history",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "history",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "auto"
          }
        }
      ]
    },
    "diCompact": {
      "h": 37,
      "radius": 19,
      "bg": "black",
      "vibrant": false,
      "ship": true,
      "els": [
        {
          "id": "leaf",
          "x": 16,
          "y": 8,
          "w": 21,
          "h": 21,
          "scale": 1,
          "weight": 600,
          "color": "leaf",
          "align": "left",
          "visible": true,
          "kind": "svg-path",
          "preset": "leafIcon",
          "min_renderer": "1.0.0"
        },
        {
          "id": "openPnl",
          "x": 192,
          "y": 9,
          "w": 50,
          "h": 19,
          "scale": 0.85,
          "weight": 700,
          "color": "auto",
          "align": "right",
          "visible": true,
          "arrows": false,
          "kind": "text",
          "preset": "openPnlSum",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "openPnlSum",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "auto"
          }
        }
      ]
    },
    "diMin": {
      "h": 37,
      "radius": 19,
      "bg": "black",
      "vibrant": false,
      "ship": true,
      "els": [
        {
          "id": "ring",
          "x": 3,
          "y": 3,
          "w": 31,
          "h": 31,
          "scale": 1,
          "weight": 700,
          "color": "leaf",
          "align": "left",
          "visible": true,
          "kind": "svg-path",
          "preset": "ring",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "topOdds",
            "min_renderer": "1.0.0",
            "scale": "fraction",
            "tone": "leaf"
          }
        }
      ]
    },
    "standby": {
      "h": 393,
      "radius": 0,
      "bg": "black",
      "vibrant": false,
      "ship": true,
      "els": [
        {
          "id": "brand",
          "x": 48,
          "y": 40,
          "w": 200,
          "h": 26,
          "scale": 1,
          "weight": 800,
          "color": "leaf",
          "align": "left",
          "visible": true,
          "tracking": 3,
          "kind": "text",
          "preset": "brand",
          "min_renderer": "1.0.0"
        },
        {
          "id": "total",
          "x": 560,
          "y": 36,
          "w": 250,
          "h": 36,
          "scale": 1,
          "weight": 600,
          "color": "ink",
          "align": "right",
          "visible": true,
          "kind": "text",
          "preset": "total",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "total",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "ink"
          }
        },
        {
          "id": "hero",
          "x": 48,
          "y": 110,
          "w": 460,
          "h": 190,
          "scale": 1,
          "weight": 800,
          "color": "auto",
          "align": "left",
          "visible": true,
          "arrows": true,
          "kind": "text",
          "preset": "heroPnl",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "heroPnl",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "auto"
          }
        },
        {
          "id": "symbols",
          "x": 560,
          "y": 110,
          "w": 120,
          "h": 170,
          "scale": 1,
          "weight": 600,
          "color": "ink",
          "align": "left",
          "visible": true,
          "maxRows": 3,
          "kind": "text",
          "preset": "symbols",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "symbols",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "ink"
          }
        },
        {
          "id": "pnl",
          "x": 680,
          "y": 110,
          "w": 130,
          "h": 170,
          "scale": 1,
          "weight": 700,
          "color": "auto",
          "align": "right",
          "visible": true,
          "arrows": true,
          "maxRows": 3,
          "kind": "text",
          "preset": "pnl",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "pnl",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "auto"
          }
        },
        {
          "id": "streak",
          "x": 48,
          "y": 330,
          "w": 760,
          "h": 18,
          "scale": 1,
          "weight": 600,
          "color": "ink",
          "align": "left",
          "visible": true,
          "count": 10,
          "kind": "text",
          "preset": "streak",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "streak",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "ink"
          }
        }
      ]
    },
    "lockRect": {
      "h": 72,
      "radius": 14,
      "bg": "none",
      "vibrant": true,
      "ship": true,
      "els": [
        {
          "id": "total",
          "x": 0,
          "y": 0,
          "w": 170,
          "h": 20,
          "scale": 1,
          "weight": 600,
          "color": "ink",
          "align": "left",
          "visible": true,
          "kind": "text",
          "preset": "total",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "total",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "ink"
          }
        },
        {
          "id": "symbols",
          "x": 0,
          "y": 24,
          "w": 84,
          "h": 46,
          "scale": 1,
          "weight": 600,
          "color": "ink",
          "align": "left",
          "visible": true,
          "maxRows": 2,
          "kind": "text",
          "preset": "symbols",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "symbols",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "ink"
          }
        },
        {
          "id": "pnl",
          "x": 86,
          "y": 24,
          "w": 84,
          "h": 46,
          "scale": 1,
          "weight": 700,
          "color": "auto",
          "align": "right",
          "visible": true,
          "arrows": true,
          "maxRows": 2,
          "kind": "text",
          "preset": "pnl",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "pnl",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "auto"
          }
        }
      ]
    },
    "lockCirc": {
      "h": 72,
      "radius": 36,
      "bg": "none",
      "vibrant": true,
      "ship": true,
      "els": [
        {
          "id": "ring",
          "x": 4,
          "y": 4,
          "w": 64,
          "h": 64,
          "scale": 1,
          "weight": 700,
          "color": "leaf",
          "align": "left",
          "visible": true,
          "kind": "svg-path",
          "preset": "ring",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "winRate",
            "min_renderer": "1.0.0",
            "scale": "fraction",
            "tone": "leaf",
            "window": "today-utc"
          }
        }
      ]
    },
    "lockInline": {
      "h": 22,
      "radius": 0,
      "bg": "none",
      "vibrant": true,
      "ship": true,
      "els": [
        {
          "id": "count",
          "x": 0,
          "y": 3,
          "w": 64,
          "h": 16,
          "scale": 1,
          "weight": 500,
          "color": "ink",
          "align": "left",
          "visible": true,
          "kind": "text",
          "preset": "openCount",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "openCount",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "ink"
          }
        },
        {
          "id": "openPnl",
          "x": 66,
          "y": 3,
          "w": 120,
          "h": 16,
          "scale": 1,
          "weight": 700,
          "color": "auto",
          "align": "left",
          "visible": true,
          "arrows": false,
          "kind": "text",
          "preset": "openPnlSum",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "openPnlSum",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "auto"
          }
        }
      ]
    },
    "homeS": {
      "h": 170,
      "radius": 22,
      "bg": "black",
      "vibrant": false,
      "ship": true,
      "els": [
        {
          "id": "brand",
          "x": 14,
          "y": 14,
          "w": 110,
          "h": 13,
          "scale": 1,
          "weight": 800,
          "color": "leaf",
          "align": "left",
          "visible": true,
          "tracking": 3,
          "kind": "text",
          "preset": "brand",
          "min_renderer": "1.0.0"
        },
        {
          "id": "total",
          "x": 14,
          "y": 34,
          "w": 142,
          "h": 26,
          "scale": 1,
          "weight": 600,
          "color": "ink",
          "align": "left",
          "visible": true,
          "kind": "text",
          "preset": "total",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "total",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "ink"
          }
        },
        {
          "id": "today",
          "x": 14,
          "y": 64,
          "w": 142,
          "h": 15,
          "scale": 1,
          "weight": 600,
          "color": "auto",
          "align": "left",
          "visible": true,
          "showPct": true,
          "kind": "text",
          "preset": "todayPnl",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "todayPnl",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "auto"
          }
        },
        {
          "id": "spark",
          "x": 14,
          "y": 88,
          "w": 142,
          "h": 44,
          "scale": 1,
          "weight": 600,
          "color": "auto",
          "align": "left",
          "visible": true,
          "kind": "svg-path",
          "preset": "spark",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "spark",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "auto"
          }
        },
        {
          "id": "streak",
          "x": 14,
          "y": 144,
          "w": 142,
          "h": 12,
          "scale": 1,
          "weight": 600,
          "color": "ink",
          "align": "left",
          "visible": true,
          "count": 8,
          "kind": "text",
          "preset": "streak",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "streak",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "ink"
          }
        }
      ]
    },
    "homeM": {
      "h": 170,
      "radius": 22,
      "bg": "black",
      "vibrant": false,
      "ship": true,
      "els": [
        {
          "id": "brand",
          "x": 16,
          "y": 14,
          "w": 120,
          "h": 14,
          "scale": 1,
          "weight": 800,
          "color": "leaf",
          "align": "left",
          "visible": true,
          "tracking": 3,
          "kind": "text",
          "preset": "brand",
          "min_renderer": "1.0.0"
        },
        {
          "id": "total",
          "x": 214,
          "y": 12,
          "w": 134,
          "h": 20,
          "scale": 1,
          "weight": 600,
          "color": "ink",
          "align": "right",
          "visible": true,
          "kind": "text",
          "preset": "total",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "total",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "ink"
          }
        },
        {
          "id": "symbols",
          "x": 16,
          "y": 42,
          "w": 110,
          "h": 84,
          "scale": 1,
          "weight": 600,
          "color": "ink",
          "align": "left",
          "visible": true,
          "maxRows": 3,
          "kind": "text",
          "preset": "symbols",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "symbols",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "ink"
          }
        },
        {
          "id": "odds",
          "x": 128,
          "y": 42,
          "w": 56,
          "h": 84,
          "scale": 1,
          "weight": 600,
          "color": "ink",
          "align": "right",
          "visible": true,
          "maxRows": 3,
          "kind": "text",
          "preset": "odds",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "odds",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "ink"
          }
        },
        {
          "id": "oddsbar",
          "x": 190,
          "y": 42,
          "w": 60,
          "h": 84,
          "scale": 1,
          "weight": 600,
          "color": "leaf",
          "align": "left",
          "visible": true,
          "maxRows": 3,
          "kind": "box",
          "preset": "oddsBar",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "oddsBar",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "leaf"
          }
        },
        {
          "id": "pnl",
          "x": 254,
          "y": 42,
          "w": 94,
          "h": 84,
          "scale": 1,
          "weight": 700,
          "color": "auto",
          "align": "right",
          "visible": true,
          "arrows": true,
          "maxRows": 3,
          "kind": "text",
          "preset": "pnl",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "pnl",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "auto"
          }
        },
        {
          "id": "history",
          "x": 16,
          "y": 134,
          "w": 332,
          "h": 30,
          "scale": 1,
          "weight": 500,
          "color": "auto",
          "align": "left",
          "visible": true,
          "count": 4,
          "kind": "text",
          "preset": "history",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "history",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "auto"
          }
        }
      ]
    },
    "homeL": {
      "h": 382,
      "radius": 22,
      "bg": "black",
      "vibrant": false,
      "ship": false,
      "els": [
        {
          "id": "brand",
          "x": 18,
          "y": 18,
          "w": 130,
          "h": 16,
          "scale": 1,
          "weight": 800,
          "color": "leaf",
          "align": "left",
          "visible": true,
          "tracking": 3,
          "kind": "text",
          "preset": "brand",
          "min_renderer": "1.0.0"
        },
        {
          "id": "live",
          "x": 294,
          "y": 18,
          "w": 52,
          "h": 14,
          "scale": 1,
          "weight": 700,
          "color": "leaf",
          "align": "left",
          "visible": true,
          "kind": "text",
          "preset": "live",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "live",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "leaf"
          }
        },
        {
          "id": "total",
          "x": 18,
          "y": 44,
          "w": 220,
          "h": 36,
          "scale": 1,
          "weight": 600,
          "color": "ink",
          "align": "left",
          "visible": true,
          "kind": "text",
          "preset": "total",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "total",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "ink"
          }
        },
        {
          "id": "today",
          "x": 18,
          "y": 84,
          "w": 220,
          "h": 18,
          "scale": 1,
          "weight": 600,
          "color": "auto",
          "align": "left",
          "visible": true,
          "showPct": true,
          "kind": "text",
          "preset": "todayPnl",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "todayPnl",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "auto"
          }
        },
        {
          "id": "spark",
          "x": 18,
          "y": 112,
          "w": 328,
          "h": 70,
          "scale": 1,
          "weight": 600,
          "color": "auto",
          "align": "left",
          "visible": true,
          "kind": "svg-path",
          "preset": "spark",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "spark",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "auto"
          }
        },
        {
          "id": "symbols",
          "x": 18,
          "y": 196,
          "w": 100,
          "h": 90,
          "scale": 1,
          "weight": 600,
          "color": "ink",
          "align": "left",
          "visible": true,
          "maxRows": 3,
          "kind": "text",
          "preset": "symbols",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "symbols",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "ink"
          }
        },
        {
          "id": "odds",
          "x": 120,
          "y": 196,
          "w": 50,
          "h": 90,
          "scale": 1,
          "weight": 600,
          "color": "ink",
          "align": "right",
          "visible": true,
          "maxRows": 3,
          "kind": "text",
          "preset": "odds",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "odds",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "ink"
          }
        },
        {
          "id": "towin",
          "x": 176,
          "y": 196,
          "w": 80,
          "h": 90,
          "scale": 1,
          "weight": 600,
          "color": "leaf",
          "align": "right",
          "visible": true,
          "maxRows": 3,
          "kind": "text",
          "preset": "toWin",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "toWin",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "leaf"
          }
        },
        {
          "id": "pnl",
          "x": 258,
          "y": 196,
          "w": 88,
          "h": 90,
          "scale": 1,
          "weight": 700,
          "color": "auto",
          "align": "right",
          "visible": true,
          "arrows": true,
          "maxRows": 3,
          "kind": "text",
          "preset": "pnl",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "pnl",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "auto"
          }
        },
        {
          "id": "divider",
          "x": 18,
          "y": 294,
          "w": 328,
          "h": 1,
          "scale": 1,
          "weight": 600,
          "color": "slate",
          "align": "left",
          "visible": true,
          "opacity": 0.35,
          "kind": "box",
          "preset": "divider",
          "min_renderer": "1.0.0"
        },
        {
          "id": "hlist",
          "x": 18,
          "y": 302,
          "w": 328,
          "h": 62,
          "scale": 1,
          "weight": 600,
          "color": "auto",
          "align": "left",
          "visible": true,
          "count": 3,
          "kind": "text",
          "preset": "historyList",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "historyList",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "auto"
          }
        }
      ]
    },
    "watchStack": {
      "h": 88,
      "radius": 22,
      "bg": "black",
      "vibrant": false,
      "ship": false,
      "els": [
        {
          "id": "leaf",
          "x": 12,
          "y": 10,
          "w": 16,
          "h": 16,
          "scale": 1,
          "weight": 600,
          "color": "leaf",
          "align": "left",
          "visible": true,
          "kind": "svg-path",
          "preset": "leafIcon",
          "min_renderer": "1.0.0"
        },
        {
          "id": "total",
          "x": 34,
          "y": 9,
          "w": 142,
          "h": 18,
          "scale": 0.9,
          "weight": 600,
          "color": "ink",
          "align": "right",
          "visible": true,
          "kind": "text",
          "preset": "total",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "total",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "ink"
          }
        },
        {
          "id": "symbols",
          "x": 12,
          "y": 32,
          "w": 80,
          "h": 50,
          "scale": 1,
          "weight": 600,
          "color": "ink",
          "align": "left",
          "visible": true,
          "maxRows": 2,
          "kind": "text",
          "preset": "symbols",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "symbols",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "ink"
          }
        },
        {
          "id": "pnl",
          "x": 94,
          "y": 32,
          "w": 82,
          "h": 50,
          "scale": 1,
          "weight": 700,
          "color": "auto",
          "align": "right",
          "visible": true,
          "arrows": true,
          "maxRows": 2,
          "kind": "text",
          "preset": "pnl",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "pnl",
            "min_renderer": "1.0.0",
            "scale": "identity",
            "tone": "auto"
          }
        }
      ]
    },
    "watchCorner": {
      "h": 62,
      "radius": 31,
      "bg": "none",
      "vibrant": true,
      "ship": false,
      "els": [
        {
          "id": "ring",
          "x": 2,
          "y": 2,
          "w": 58,
          "h": 58,
          "scale": 1,
          "weight": 700,
          "color": "leaf",
          "align": "left",
          "visible": true,
          "kind": "svg-path",
          "preset": "ring",
          "min_renderer": "1.0.0",
          "binding": {
            "source": "kashbot.board",
            "field": "todayPct",
            "min_renderer": "1.0.0",
            "scale": "today-percent",
            "tone": "leaf"
          }
        }
      ]
    }
  }
})
