"""Closed optional ENV1 configuration, without mutation or default injection."""
import math
import re
import unicodedata


def choice(*values):
    return lambda value: type(value) is str and value in values


def integer(low, high):
    return lambda value: type(value) is int and low <= value <= high


def color(value):
    return type(value) is str and re.fullmatch(r'#[0-9a-fA-F]{6}', value) is not None


def yes(value):
    return value is True


def text(high):
    return lambda value: (type(value) is str and 1 <= len(value) <= high and bool(value.strip())
                          and not any(c != '\n' and unicodedata.category(c) in ('Cc', 'Cf', 'Cs') for c in value)
                          and '<' not in value and '>' not in value
                          and not re.search(r'(?:[a-z][a-z0-9+.-]*://|\b(?:https?|ftp|file|data|javascript):|\bwww\.)', value, re.I))


def validate_design(value, rules):
    if (type(value) is not dict or not value
            or any(key not in rules or not rules[key](item) for key, item in value.items())):
        raise ValueError('Unsupported page design')
    return True


def obj(rules):
    return lambda value: validate_design(value, rules)


FONT = choice('classic', 'serif', 'sans', 'mono')
ALIGN = choice('left', 'center', 'right')
WEIGHT = choice('regular', 'medium', 'bold')
ASPECT = choice('natural', 'square', 'landscape', 'portrait')
FOCAL = choice('center', 'top', 'bottom', 'left', 'right', 'top-left', 'top-right', 'bottom-left', 'bottom-right')
FIT = choice('contain', 'cover')
TEXT = dict(font=FONT, size=integer(12, 96), weight=WEIGHT, color=color, align=ALIGN,
            lineHeight=lambda v: type(v) in (int, float) and 1 <= v <= 2.5 and math.isfinite(v),
            letterSpacing=integer(-1, 8), italic=yes, underline=yes, hidden=yes)
CONTAINER = dict(align=ALIGN, columns=integer(1, 4), mobileColumns=integer(1, 2), gap=integer(0, 96),
                 padding=integer(0, 96), maxWidth=integer(240, 1800), background=color,
                 radius=integer(0, 64), borderWidth=integer(0, 8), borderColor=color,
                 shadow=choice('none', 'soft', 'strong'), hidden=yes)
PHOTO = dict(fit=FIT, aspect=ASPECT, focal=FOCAL, span=integer(1, 2), radius=integer(0, 64), hidden=yes)
PAGE_APPEARANCE = dict(font=FONT, scale=choice('compact', 'regular', 'large'),
                       spacing=choice('tight', 'regular', 'airy'), width=choice('narrow', 'regular', 'wide'),
                       palette=obj(dict.fromkeys(('background', 'text', 'muted', 'accent'), color)),
                       **{key: CONTAINER[key] for key in ('gap', 'padding', 'maxWidth')})
PORTFOLIO_APPEARANCE = dict(PAGE_APPEARANCE, headingWeight=WEIGHT, align=ALIGN,
                            columns=CONTAINER['columns'], mobileColumns=CONTAINER['mobileColumns'],
                            imageFit=FIT, aspect=ASPECT, focal=FOCAL)


def nav_order(value):
    return (type(value) is list and all(type(item) is str and item in ('home', 'galleries', 'about', 'resume') for item in value)
            and len(set(value)) == len(value))


NAV = dict(homeLabel=text(40), aboutLabel=text(40), resumeLabel=text(40), order=nav_order,
           hideHome=yes, hideAbout=yes, hideResume=yes, hidden=yes, layout=choice('row', 'column'),
           appearance=obj({key: rule for key, rule in TEXT.items() if key != 'hidden'}),
           **{key: CONTAINER[key] for key in ('gap', 'padding', 'background', 'radius')})


def styles(names):
    return obj(dict.fromkeys(names.split(), obj(TEXT)))


FIELDS = {
    'page': dict(appearance=obj(PAGE_APPEARANCE), textStyles=styles('label headline description status')),
    'portfolio': dict(appearance=obj(PORTFOLIO_APPEARANCE),
                      textStyles=styles('name headline introduction about aboutHeading contactHeading location email resumeHeading'),
                      nav=obj(NAV), labels=obj(dict.fromkeys(('aboutHeading', 'contactHeading', 'resumeHeading', 'viewerClose'), text(80)))),
    'category': dict(layout=obj(CONTAINER), hidden=yes, navLabel=text(40), textStyles=styles('title description navigation')),
    'image': dict(display=obj(PHOTO), captionStyle=obj(TEXT)),
    'section': dict(layout=obj(CONTAINER), headingStyle=obj(TEXT)),
    'entry': dict(layout=obj(CONTAINER), textStyles=styles('title subtitle dates description')),
}


def validate_node(node, kind):
    for key, rule in FIELDS[kind].items():
        if key in node and not rule(node[key]):
            raise ValueError(f'Unsupported {key}')


# Constant source, shared by the two portable web producers. User data stays JSON.
DESIGN_JS = """export const fonts={classic:'Georgia, serif',serif:'Georgia, serif',sans:'Arial, sans-serif',mono:'monospace'};
export function textStyle(v={}){return {fontFamily:fonts[v.font],fontSize:v.size,fontWeight:{regular:400,medium:500,bold:700}[v.weight],color:v.color,textAlign:v.align,lineHeight:v.lineHeight,letterSpacing:v.letterSpacing,fontStyle:v.italic?'italic':undefined,textDecoration:v.underline?'underline':undefined,display:v.hidden?'none':v.align||v.size||v.lineHeight?'block':undefined};}
export function containerStyle(v={}){return {textAlign:v.align,'--design-columns':v.columns,'--design-mobile-columns':v.mobileColumns,'--design-gap':v.gap===undefined?undefined:`${v.gap}px`,gap:v.gap,padding:v.padding,maxWidth:v.maxWidth===undefined?undefined:`min(100%, ${v.maxWidth}px)`,background:v.background,borderRadius:v.radius,borderWidth:v.borderWidth,borderStyle:v.borderWidth===undefined?undefined:'solid',borderColor:v.borderColor,boxShadow:{none:'none',soft:'0 2px 12px #0002',strong:'0 8px 28px #0005'}[v.shadow],display:v.hidden?'none':undefined};}
export function photoStyle(v={},a={}){const aspect=v.aspect??a.aspect;return {objectFit:v.fit??a.imageFit,objectPosition:(v.focal??a.focal)?.replace('-',' '),aspectRatio:{natural:'auto',square:'1',landscape:'4 / 3',portrait:'3 / 4'}[aspect],height:aspect?'auto':undefined,borderRadius:v.radius,display:v.hidden?'none':undefined};}
export function appearanceStyle(a={}){const p=a.palette||{};return {...containerStyle(a),fontFamily:a.font==='classic'?'Arial, sans-serif':fonts[a.font],'--design-scale':{compact:.85,regular:1,large:1.2}[a.scale],'--design-spacing':{tight:'16px',regular:'32px',airy:'48px'}[a.spacing],'--design-weight':{regular:400,medium:500,bold:700}[a.headingWeight],'--design-width':{narrow:'720px',regular:'1200px',wide:'1600px'}[a.width],'--design-font':fonts[a.font],'--page-bg':p.background,'--page-fg':p.text,'--page-muted':p.muted,'--design-accent':p.accent,background:p.background,color:p.text};}
export function navigationOrder(n={}){return [...(n.order||[]),...['home','galleries','about','resume'].filter(k=>!n.order?.includes(k))];}
export function photoSpanStyle(v={},page,c){if(v.span===undefined)return {};const columns=c?.layout?.columns??page.appearance?.columns??(page.galleryLayout==='single'?1:2),mobile=c?.layout?.mobileColumns??page.appearance?.mobileColumns??1;return {gridColumn:`span ${Math.min(v.span,columns)}`,'--design-mobile-span':Math.min(v.span,mobile)};}
export function galleryStyle(page,c){return {'--design-mobile-columns':c?.layout?.mobileColumns??page.appearance?.mobileColumns,gap:c?.layout?.gap??page.appearance?.gap,'--design-columns':c?.layout?.columns??page.appearance?.columns??(c&&page.galleryLayout==='single'?1:2)};}
"""
DESIGN_CSS = """
.portfolio{font-size:calc(16px * var(--design-scale,1))}
.portfolio h1{font-family:var(--design-font,Georgia,serif);font-weight:var(--design-weight,400);font-size:calc(clamp(32px,5vw,64px) * var(--design-scale,1))}
.portfolio>:not(nav):not(.portfolio-viewer){max-width:var(--design-width,none)}
.portfolio .portfolio-grid,.portfolio .portfolio-grid[data-gallery-layout="single"]{grid-template-columns:repeat(var(--design-columns,2),minmax(0,1fr));gap:var(--design-spacing,32px)}
.design-container{display:grid;grid-template-columns:repeat(var(--design-columns,1),minmax(0,1fr))}
.portfolio nav{color:var(--design-accent,inherit)}.portfolio p{color:var(--page-muted,inherit)}
.project-page__content{max-width:var(--design-width,48rem);display:flex;flex-direction:column;gap:var(--design-spacing,0)}
.project-page .project-page__headline{font-size:calc(clamp(2.5rem,8vw,5.5rem) * var(--design-scale,1))}
.project-page__label,.project-page__status{font-family:var(--design-font,Arial,sans-serif)}
.portfolio h2,.portfolio h3{font-family:var(--design-font,inherit);font-weight:var(--design-weight,bold)}
.portfolio .portfolio-grid,.design-container{gap:var(--design-gap,var(--design-spacing,32px))}
.portfolio-grid>*{min-width:0}
.portfolio-grid button,.portfolio-grid a{text-align:inherit}
.project-page__content{width:min(100%,var(--design-width,48rem));gap:var(--design-gap,var(--design-spacing,0))}
.project-page__label,.project-page__status{font-size:calc(.875rem * var(--design-scale,1))}
.project-page__description{font-size:calc(clamp(1rem,2.5vw,1.25rem) * var(--design-scale,1))}
.project-page__status{color:var(--design-accent,var(--page-muted))}
@media(max-width:600px){.portfolio .portfolio-grid,.portfolio .portfolio-grid[data-gallery-layout="single"],.design-container{grid-template-columns:repeat(var(--design-mobile-columns,1),minmax(0,1fr))}.portfolio figure{grid-column:span var(--design-mobile-span,1)!important}}
"""
