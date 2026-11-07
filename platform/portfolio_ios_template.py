"""Portable, bounded native SwiftUI portfolio source; no filesystem or network IO.

The caller supplies authorized, verified preview bytes. This producer repeats
structural and digest checks but does not establish project authorization.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import unicodedata
import zipfile

if __package__:
    from .page_design import FIELDS as DESIGN_FIELDS, validate_node
else:
    import importlib.util
    from pathlib import Path
    _design_spec = importlib.util.spec_from_file_location('page_design', Path(__file__).with_name('page_design.py'))
    _design = importlib.util.module_from_spec(_design_spec)
    _design_spec.loader.exec_module(_design)
    DESIGN_FIELDS, validate_node = _design.FIELDS, _design.validate_node


MAX_DOCUMENT_BYTES = 512 * 1024
MAX_ASSET_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 80 * 1024 * 1024
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_SLUG = re.compile(r"[a-z][a-z0-9-]{0,47}\Z")
_EMAIL = re.compile(r"[A-Za-z0-9]+(?:[._+-][A-Za-z0-9]+)*@[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?:\.[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)+\Z")
_URL = re.compile(r"(?:[a-z][a-z0-9+.-]*://|\b(?:https?|ftp|file|data|javascript):|\bwww\.)", re.I)


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _keys(value: object, names: str, optional=()) -> None:
    if type(value) is not dict or set(value) - set(optional) != set(names.split()):
        raise ValueError("Portfolio object has missing or unsupported fields")


def _text(value: object, minimum: int, maximum: int) -> None:
    if type(value) is not str or not minimum <= len(value) <= maximum:
        raise ValueError("Invalid portfolio text length")
    if minimum and not value.strip():
        raise ValueError("Required portfolio text is blank")
    if any(c != "\n" and unicodedata.category(c) in ("Cc", "Cf", "Cs") for c in value):
        raise ValueError("Portfolio text contains control characters")
    if "<" in value or ">" in value or _URL.search(value):
        raise ValueError("Portfolio text must not contain HTML or URLs")


def _items(value: object, minimum: int, maximum: int) -> None:
    if type(value) is not list or not minimum <= len(value) <= maximum:
        raise ValueError("Invalid portfolio collection size")


def validate_document(document: dict) -> dict:
    """Return detached F14 data; reject unknown fields, unsafe refs and bounds."""
    _keys(document, "schema name headline introduction about contact resume categories theme", ("galleryLayout", *DESIGN_FIELDS['portfolio']))
    validate_node(document, 'portfolio')
    if "galleryLayout" in document and (type(document["galleryLayout"]) is not str or document["galleryLayout"] not in ("grid", "single")):
        raise ValueError("Unsupported photo layout")
    if document["schema"] != "leaf.portfolio.v1" or document["theme"] != "light":
        raise ValueError("Unsupported portfolio schema or theme")
    for key, low, high in (("name", 1, 120), ("headline", 1, 240),
                           ("introduction", 0, 2400), ("about", 0, 8000)):
        _text(document[key], low, high)
    contact = document["contact"]
    _keys(contact, "email location")
    _text(contact["location"], 0, 160)
    email = contact["email"]
    if type(email) is not str or len(email) > 254 or (email and not _EMAIL.fullmatch(email)):
        raise ValueError("Invalid contact email")
    _items(document["resume"], 0, 8)
    for section in document["resume"]:
        _keys(section, "heading entries", DESIGN_FIELDS['section'])
        validate_node(section, 'section')
        _text(section["heading"], 1, 120)
        _items(section["entries"], 0, 20)
        for entry in section["entries"]:
            _keys(entry, "title subtitle dates description", DESIGN_FIELDS['entry'])
            validate_node(entry, 'entry')
            for key, high in (("title", 200), ("subtitle", 240), ("dates", 120), ("description", 2400)):
                _text(entry[key], 0, high)
    _items(document["categories"], 1, 12)
    slugs, references, total = set(), {}, 0
    for category in document["categories"]:
        _keys(category, "slug title description hero images", DESIGN_FIELDS['category'])
        validate_node(category, 'category')
        slug = category["slug"]
        if (type(slug) is not str or not _SLUG.fullmatch(slug)
                or slug in {"home", "about", "resume"} or slug in slugs):
            raise ValueError("Invalid or duplicate category slug")
        slugs.add(slug)
        _text(category["title"], 1, 120)
        _text(category["description"], 0, 1200)
        _items(category["images"], 1, 200)
        total += len(category["images"])
        if total > 200:
            raise ValueError("Portfolio has more than 200 image references")
        hero = category["hero"]
        if type(hero) is not int or not 0 <= hero < len(category["images"]):
            raise ValueError("Invalid category hero index")
        seen = set()
        for image in category["images"]:
            _keys(image, "material_id sha256 preview_sha256 title alt", DESIGN_FIELDS['image'])
            validate_node(image, 'image')
            material_id = image["material_id"]
            if type(material_id) is not str or not _UUID.fullmatch(material_id) or material_id in seen:
                raise ValueError("Invalid or duplicate material ID")
            seen.add(material_id)
            for key in ("sha256", "preview_sha256"):
                if type(image[key]) is not str or not _SHA.fullmatch(image[key]):
                    raise ValueError("Invalid material digest")
            identity = (image["sha256"], image["preview_sha256"])
            if material_id in references and references[material_id] != identity:
                raise ValueError("Conflicting digests for reused material")
            references[material_id] = identity
            _text(image["title"], 0, 200)
            _text(image["alt"], 1, 400)
    encoded = _canonical_json(document)
    if len(encoded) > MAX_DOCUMENT_BYTES:
        raise ValueError("Portfolio document exceeds 512KiB")
    return json.loads(encoded)


def document_digest(document: dict) -> str:
    return hashlib.sha256(_canonical_json(validate_document(document))).hexdigest()


def source_archive(document: dict, revision: int, assets: dict[str, bytes]) -> bytes:
    """Produce deterministic source bytes, without compiling or signing an app.

    Assets are JPEG previews keyed by canonical material UUID. No supplied
    text, identifier or path is interpolated into Swift or project settings.
    """
    if type(revision) is not int or not 1 <= revision <= 9007199254740991:
        raise ValueError("Revision must be a positive safe integer")
    page = validate_document(document)
    refs = {image["material_id"]: image for category in page["categories"]
            for image in category["images"]}
    if type(assets) is not dict or set(assets) != set(refs):
        raise ValueError("Assets must exactly match referenced material IDs")
    total = 0
    for material_id, data in assets.items():
        if type(data) is not bytes or not data:
            raise ValueError("Each asset must contain nonempty JPEG bytes")
        total += len(data)
        if total > MAX_ASSET_BYTES:
            raise ValueError("Portfolio previews exceed 64MiB")
        if hashlib.sha256(data).hexdigest() != refs[material_id]["preview_sha256"]:
            raise ValueError("Preview SHA256 does not match portfolio document")
    payload = _canonical_json(page)
    files = {
        "Portfolio/PortfolioApp.swift": _SWIFT.encode("utf-8"),
        "Portfolio/PortfolioContent/portfolio.json": payload + b"\n",
        "Portfolio/Info.plist": _INFO_PLIST.encode("utf-8"),
        "Portfolio.xcodeproj/project.pbxproj": _PROJECT.encode("utf-8"),
        "Portfolio.xcodeproj/xcshareddata/xcschemes/Portfolio.xcscheme": _SCHEME.encode("utf-8"),
        "README.md": _README.encode("utf-8"),
        ".gitignore": b"build/\nDerivedData/\nxcuserdata/\n*.xcuserstate\n.DS_Store\n",
    }
    asset_manifest = {}
    for material_id in sorted(refs):
        path = "Portfolio/PortfolioContent/images/" + material_id + ".jpg"
        files[path] = assets[material_id]
        asset_manifest[material_id] = {"path": path, "sha256": refs[material_id]["sha256"],
                                       "preview_sha256": refs[material_id]["preview_sha256"]}
    manifest = {
        "schema": "leaf.portfolio-ios-source.v1", "revision": revision,
        "document_sha256": hashlib.sha256(payload).hexdigest(),
        "assets": asset_manifest,
        "files": {path: {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
                  for path, data in sorted(files.items())},
    }
    files["source-manifest.json"] = _canonical_json(manifest) + b"\n"
    # ZIP_STORED is portable and byte-stable across zlib versions. JPEGs are
    # already compressed; the bounded source adds only a small fixed overhead.
    if sum(map(len, files.values())) > MAX_ARCHIVE_BYTES:
        raise ValueError("Native source archive exceeds 80MiB")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for path, data in sorted(files.items()):
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_STORED
            archive.writestr(info, data)
    result = output.getvalue()
    if len(result) > MAX_ARCHIVE_BYTES:
        raise ValueError("Native source archive exceeds 80MiB")
    return result


_SWIFT = r'''import SwiftUI
import UIKit

struct PortfolioDocument: Decodable {
    let schema: String
    let name: String
    let headline: String
    let introduction: String
    let about: String
    let contact: Contact
    let resume: [ResumeSection]
    let categories: [PortfolioCategory]
    let theme: String
    let galleryLayout: String?
    let appearance: Design?
    let textStyles: [String: Design]?
    let labels: [String: String]?
    let nav: NavigationDesign?
}
struct Contact: Decodable { let email: String; let location: String }
struct ResumeSection: Decodable {
    let heading: String; let entries: [ResumeEntry]
    let layout: Design?; let headingStyle: Design?
}
struct ResumeEntry: Decodable {
    let title: String; let subtitle: String; let dates: String; let description: String
    let layout: Design?; let textStyles: [String: Design]?
}
struct PortfolioCategory: Decodable, Identifiable {
    let slug: String; let title: String; let description: String
    let hero: Int; let images: [PortfolioPhoto]
    let layout: Design?; let hidden: Bool?; let navLabel: String?
    let textStyles: [String: Design]?
    var id: String { slug }
    var visible: Bool { hidden != true && layout?.hidden != true }
}
struct PortfolioPhoto: Decodable, Identifiable {
    let materialId: String; let sha256: String; let previewSha256: String
    let title: String; let alt: String
    let display: Design?; let captionStyle: Design?
    var id: String { materialId }
}

// All design fields remain optional: decoding a legacy archive supplies no overrides.
struct Design: Decodable {
    var font: String?; var size: Double?; var weight: String?; var color: String?
    var align: String?; var lineHeight: Double?; var letterSpacing: Double?
    var italic: Bool?; var underline: Bool?; var hidden: Bool?
    var columns: Int?; var mobileColumns: Int?; var gap: Double?; var padding: Double?
    var maxWidth: Double?; var background: String?; var radius: Double?
    var borderWidth: Double?; var borderColor: String?; var shadow: String?
    var fit: String?; var imageFit: String?; var aspect: String?; var focal: String?; var span: Int?
    var scale: String?; var headingWeight: String?; var spacing: String?; var width: String?
    var palette: [String: String]?
    var resolvedGap: CGFloat { CGFloat(gap ?? (spacing == "tight" ? 16 : spacing == "airy" ? 48 : spacing == "regular" ? 32 : 24)) }
    var resolvedWidth: CGFloat { CGFloat(maxWidth ?? (width == "narrow" ? 720 : width == "wide" ? 1600 : 1200)) }
    var gridGap: CGFloat { gap != nil || spacing != nil ? resolvedGap : 32 }
    var gridColumnGap: CGFloat { gap != nil || spacing != nil ? resolvedGap : 24 }
    func inheritingText(_ parent: Design?) -> Design {
        var result = self
        result.font = font ?? parent?.font; result.size = size ?? parent?.size
        result.weight = weight ?? parent?.weight; result.color = color ?? parent?.color
        result.align = align ?? parent?.align; result.lineHeight = lineHeight ?? parent?.lineHeight
        result.letterSpacing = letterSpacing ?? parent?.letterSpacing
        result.italic = italic ?? parent?.italic; result.underline = underline ?? parent?.underline
        return result
    }
}
struct NavigationDesign: Decodable {
    let homeLabel: String?; let aboutLabel: String?; let resumeLabel: String?
    let order: [String]?; let hideHome: Bool?; let hideAbout: Bool?; let hideResume: Bool?
    let hidden: Bool?; let layout: String?; let appearance: Design?
    let gap: Double?; let padding: Double?; let background: String?; let radius: Double?
    static var empty: NavigationDesign {
        NavigationDesign(homeLabel: nil, aboutLabel: nil, resumeLabel: nil, order: nil,
            hideHome: nil, hideAbout: nil, hideResume: nil, hidden: nil, layout: nil,
            appearance: nil, gap: nil, padding: nil, background: nil, radius: nil)
    }
    var groups: [String] {
        let first = order ?? []
        return first + ["home", "galleries", "about", "resume"].filter { !first.contains($0) }
    }
}
func designColor(_ hex: String?, fallback: Color = .primary) -> Color {
    guard let hex, hex.range(of: #"^#[0-9a-fA-F]{6}$"#, options: .regularExpression) != nil,
          let number = UInt32(hex.dropFirst(), radix: 16) else { return fallback }
    return Color(red: Double((number >> 16) & 255) / 255,
                 green: Double((number >> 8) & 255) / 255, blue: Double(number & 255) / 255)
}
func designAlignment(_ value: String?) -> Alignment {
    switch value {
    case "left": return .leading
    case "right": return .trailing
    case "top": return .top
    case "bottom": return .bottom
    case "top-left": return .topLeading
    case "top-right": return .topTrailing
    case "bottom-left": return .bottomLeading
    case "bottom-right": return .bottomTrailing
    default: return .center
    }
}
func textAlignment(_ value: String?) -> TextAlignment {
    value == "center" ? .center : value == "right" ? .trailing : .leading
}
struct ContainerAlignmentKey: EnvironmentKey { static let defaultValue: String? = nil }
extension EnvironmentValues {
    var containerAlignment: String? {
        get { self[ContainerAlignmentKey.self] }
        set { self[ContainerAlignmentKey.self] = newValue }
    }
}
struct DesignedText: View {
    let value: String
    var style: Design? = nil
    var root: Design? = nil
    var size: CGFloat = 17
    var heading = false
    var muted = false
    var defaultWeight: Font.Weight = .regular
    @Environment(\.containerAlignment) private var containerAlignment
    @ScaledMetric(relativeTo: .body) private var metric: CGFloat = 1
    private var pointSize: CGFloat {
        CGFloat(style?.size ?? Double(size) * (root?.scale == "compact" ? 0.85 : root?.scale == "large" ? 1.2 : 1)) * metric
    }
    private var fontDesign: Font.Design {
        switch style?.font ?? root?.font {
        case "serif": return .serif
        case "mono": return .monospaced
        case "sans": return .default
        case "classic": return heading ? .serif : .default
        default: return .default
        }
    }
    private var weight: Font.Weight {
        switch style?.weight ?? (heading ? root?.headingWeight : nil) {
        case "bold": return .bold
        case "medium": return .medium
        case "regular": return .regular
        default: return defaultWeight
        }
    }
    var body: some View {
        if style?.hidden != true {
            Text(verbatim: value)
                .font(.system(size: pointSize, weight: weight, design: fontDesign))
                .italic(style?.italic == true).underline(style?.underline == true)
                .tracking(CGFloat(style?.letterSpacing ?? 0))
                .lineSpacing(max(0, pointSize * CGFloat((style?.lineHeight ?? 1) - 1)))
                .multilineTextAlignment(textAlignment(style?.align ?? containerAlignment ?? root?.align))
                .frame(maxWidth: .infinity, alignment: designAlignment(style?.align ?? containerAlignment ?? root?.align ?? "left"))
                .foregroundStyle(designColor(style?.color ?? root?.palette?[muted ? "muted" : "text"], fallback: muted ? .secondary : .primary))
                .accessibilityAddTraits(heading ? .isHeader : [])
        }
    }
}
struct BoxDesign: ViewModifier {
    var value: Design?
    @Environment(\.containerAlignment) private var containerAlignment
    func body(content: Content) -> some View {
        if value?.hidden != true {
            content
                .environment(\.containerAlignment, value?.align ?? containerAlignment)
                .padding(CGFloat(value?.padding ?? 0))
                .frame(maxWidth: value?.maxWidth.map { CGFloat($0) }, alignment: designAlignment(value?.align ?? "left"))
                .background(designColor(value?.background, fallback: .clear))
                .clipShape(RoundedRectangle(cornerRadius: CGFloat(value?.radius ?? 0)))
                .overlay(RoundedRectangle(cornerRadius: CGFloat(value?.radius ?? 0))
                    .stroke(designColor(value?.borderColor), lineWidth: CGFloat(value?.borderWidth ?? 0)))
                .shadow(color: .black.opacity(value?.shadow == "strong" ? 0.3 : value?.shadow == "soft" ? 0.12 : 0),
                        radius: value?.shadow == "strong" ? 14 : 6, y: value?.shadow == "strong" ? 8 : 2)
        }
    }
}
struct PhotoSpan: LayoutValueKey { static let defaultValue = 1 }
// A bounded responsive grid with real spanning cells; child order is source order.
struct DesignGrid: Layout {
    var columns: Int?
    var mobileColumns: Int?
    var gap: CGFloat = 24
    var columnGap: CGFloat? = nil
    var adaptive = false
    private func arrange(_ proposal: ProposedViewSize, _ subviews: Subviews) -> (CGSize, [CGRect]) {
        let width = max(1, proposal.width ?? 760)
        let horizontalGap = columnGap ?? gap
        let legacyCount = adaptive ? max(1, Int((width + horizontalGap) / (240 + horizontalGap))) : 1
        let count = max(1, width < 600 ? (mobileColumns ?? (columns == nil ? legacyCount : 1)) : (columns ?? legacyCount))
        let cell = max(1, (width - CGFloat(count - 1) * horizontalGap) / CGFloat(count))
        var frames: [CGRect] = [], column = 0
        var y: CGFloat = 0, rowHeight: CGFloat = 0
        for view in subviews {
            let span = min(count, max(1, view[PhotoSpan.self]))
            if column + span > count { y += rowHeight + gap; column = 0; rowHeight = 0 }
            let itemWidth = cell * CGFloat(span) + horizontalGap * CGFloat(span - 1)
            let size = view.sizeThatFits(ProposedViewSize(width: itemWidth, height: nil))
            frames.append(CGRect(x: CGFloat(column) * (cell + horizontalGap), y: y, width: itemWidth, height: size.height))
            rowHeight = max(rowHeight, size.height); column += span
        }
        return (CGSize(width: width, height: y + rowHeight), frames)
    }
    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        arrange(proposal, subviews).0
    }
    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let frames = arrange(ProposedViewSize(width: bounds.width, height: nil), subviews).1
        for (index, frame) in frames.enumerated() {
            subviews[index].place(at: CGPoint(x: bounds.minX + frame.minX, y: bounds.minY + frame.minY),
                anchor: .topLeading, proposal: ProposedViewSize(width: frame.width, height: frame.height))
        }
    }
}
struct PageSurface<Content: View>: View {
    let root: Design?
    var legacyWidth: CGFloat = 1200
    var legacySpacing: CGFloat = 24
    @ViewBuilder let content: () -> Content
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: root?.gap != nil || root?.spacing != nil ? root!.resolvedGap : legacySpacing, content: content)
                .padding(CGFloat(root?.padding ?? 24))
                .frame(maxWidth: root?.maxWidth != nil || root?.width != nil ? root!.resolvedWidth : legacyWidth,
                       alignment: designAlignment(root?.align ?? "left"))
                .frame(maxWidth: .infinity)
        }.background(designColor(root?.palette?["background"], fallback: .white))
            .tint(designColor(root?.palette?["accent"]))
    }
}
struct LoadedPortfolio {
    let document: PortfolioDocument
    let images: [String: UIImage]

    static func load() throws -> LoadedPortfolio {
        guard let url = Bundle.main.url(forResource: "portfolio", withExtension: "json",
                                        subdirectory: "PortfolioContent") else {
            throw ContentError.missingDocument
        }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let document = try decoder.decode(PortfolioDocument.self, from: Data(contentsOf: url))
        guard document.schema == "leaf.portfolio.v1", document.theme == "light",
              !document.categories.isEmpty else { throw ContentError.invalidDocument }
        var images: [String: UIImage] = [:]
        for category in document.categories {
            guard !category.images.isEmpty, category.images.indices.contains(category.hero) else {
                throw ContentError.invalidDocument
            }
            for photo in category.images where images[photo.id] == nil {
                // Never treat JSON as a path, even if a bundle is edited later.
                guard photo.id.range(of: #"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"#,
                                     options: .regularExpression) != nil,
                      let imageURL = Bundle.main.url(forResource: photo.id, withExtension: "jpg",
                                                      subdirectory: "PortfolioContent/images"),
                      let image = UIImage(contentsOfFile: imageURL.path) else {
                    throw ContentError.missingImage
                }
                images[photo.id] = image
            }
        }
        return LoadedPortfolio(document: document, images: images)
    }
}
enum ContentError: LocalizedError {
    case missingDocument, invalidDocument, missingImage
    var errorDescription: String? {
        switch self {
        case .missingDocument: return "The portfolio content file is missing from this app."
        case .invalidDocument: return "The portfolio content cannot be displayed."
        case .missingImage: return "A portfolio photograph is missing or unreadable."
        }
    }
}

@main
struct PortfolioApp: App {
    var body: some Scene {
        WindowGroup { PortfolioLoader().preferredColorScheme(.light).tint(.primary) }
    }
}
struct PortfolioLoader: View {
    @State private var portfolio: LoadedPortfolio?
    @State private var errorMessage: String?
    @State private var attempt = 0
    var body: some View {
        Group {
            if let portfolio {
                PortfolioTabs(portfolio: portfolio)
            } else if let errorMessage {
                ContentUnavailableView {
                    Label("Portfolio unavailable", systemImage: "exclamationmark.triangle")
                } description: {
                    Text(verbatim: errorMessage)
                } actions: {
                    Button("Try again") { self.errorMessage = nil; attempt += 1 }
                }
            } else {
                ProgressView("Loading portfolio…")
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.white)
        .task(id: attempt) {
            do { portfolio = try LoadedPortfolio.load() }
            catch {
                errorMessage = (error as? ContentError)?.errorDescription
                    ?? "The bundled portfolio content could not be read. Please restore the JSON and photographs and rebuild the app."
            }
        }
    }
}

struct PortfolioTabs: View {
    let portfolio: LoadedPortfolio
    @State private var route = "home"
    private var document: PortfolioDocument { portfolio.document }
    private var visibleCategories: [PortfolioCategory] { document.categories.filter { $0.visible } }
    @ViewBuilder private var destination: some View {
        if route == "about" { AboutView(document: document) }
        else if route == "resume" { ResumeView(document: document) }
        else if let category = document.categories.first(where: { $0.slug == route }) {
            GalleryView(category: category, images: portfolio.images, document: document)
        } else { HomeView(portfolio: portfolio) }
    }
    @ViewBuilder private func navigationButton(_ value: String, _ title: String, _ style: Design? = nil) -> some View {
        if style?.hidden != true {
            Button { route = value } label: {
                DesignedText(value: title, style: style?.inheritingText(document.nav?.appearance) ?? document.nav?.appearance, root: document.appearance)
            }.buttonStyle(.plain).padding(8)
        }
    }
    var body: some View {
        if document.nav != nil || document.categories.contains(where: { $0.navLabel != nil || $0.textStyles?["navigation"] != nil }) {
            let nav = document.nav ?? NavigationDesign.empty
            NavigationStack {
                VStack(spacing: 0) {
                    if nav.hidden != true {
                        let layout = nav.layout == "column" ? AnyLayout(VStackLayout(alignment: .leading, spacing: CGFloat(nav.gap ?? 12))) : AnyLayout(HStackLayout(spacing: CGFloat(nav.gap ?? 12)))
                        ScrollView(nav.layout == "column" ? .vertical : .horizontal) {
                            layout {
                                ForEach(nav.groups, id: \.self) { group in
                                    if group == "home" && nav.hideHome != true {
                                        navigationButton("home", nav.homeLabel ?? document.name, document.textStyles?["name"])
                                    } else if group == "about" && nav.hideAbout != true {
                                        navigationButton("about", nav.aboutLabel ?? "About / Contact")
                                    } else if group == "resume" && nav.hideResume != true {
                                        navigationButton("resume", nav.resumeLabel ?? "Resume")
                                    } else if group == "galleries" {
                                        ForEach(visibleCategories) { category in
                                            navigationButton(category.slug, category.navLabel ?? category.title, category.textStyles?["navigation"])
                                        }
                                    }
                                }
                            }.padding(CGFloat(nav.padding ?? 12))
                        }.frame(maxHeight: nav.layout == "column" ? 240 : nil)
                            .fixedSize(horizontal: false, vertical: nav.layout != "column")
                            .background(designColor(nav.background, fallback: .clear))
                            .clipShape(RoundedRectangle(cornerRadius: CGFloat(nav.radius ?? 0)))
                    }
                    destination
                }.background(designColor(document.appearance?.palette?["background"], fallback: .white))
            }.tint(designColor(document.appearance?.palette?["accent"]))
        } else {
            TabView {
                NavigationStack { HomeView(portfolio: portfolio) }
                    .tabItem { Label("Portfolio", systemImage: "photo.on.rectangle") }
                NavigationStack { AboutView(document: document) }
                    .tabItem { Label("About", systemImage: "person.crop.circle") }
                NavigationStack { ResumeView(document: document) }
                    .tabItem { Label("Resume", systemImage: "doc.text") }
            }.tint(designColor(document.appearance?.palette?["accent"]))
        }
    }
}
struct PortfolioImage: View {
    let photo: PortfolioPhoto
    let images: [String: UIImage]
    var root: Design? = nil
    private var ratio: CGFloat? {
        switch photo.display?.aspect ?? root?.aspect {
        case "square": return 1
        case "landscape": return 4 / 3
        case "portrait": return 3 / 4
        default: return nil
        }
    }
    var body: some View {
        if photo.display?.hidden != true {
            if let image = images[photo.id] {
                Group {
                    if let ratio {
                        GeometryReader { geometry in
                            Image(uiImage: image).resizable()
                                .aspectRatio(contentMode: (photo.display?.fit ?? root?.imageFit) == "cover" ? .fill : .fit)
                                .frame(width: geometry.size.width, height: geometry.size.height,
                                       alignment: designAlignment(photo.display?.focal ?? root?.focal))
                                .clipped()
                        }.aspectRatio(ratio, contentMode: .fit)
                    } else {
                        Image(uiImage: image).resizable().scaledToFit()
                    }
                }.clipShape(RoundedRectangle(cornerRadius: CGFloat(photo.display?.radius ?? 0)))
                    .accessibilityLabel(Text(verbatim: photo.alt))
            } else {
                Label("Photograph unavailable", systemImage: "photo.badge.exclamationmark")
                    .frame(maxWidth: .infinity, minHeight: 120)
            }
        }
    }
}
struct HomeView: View {
    let portfolio: LoadedPortfolio
    private var document: PortfolioDocument { portfolio.document }
    var body: some View {
        PageSurface(root: document.appearance) {
            DesignedText(value: document.name, style: document.textStyles?["name"], root: document.appearance, size: 20, heading: true)
            DesignedText(value: document.headline, style: document.textStyles?["headline"], root: document.appearance, size: 34, heading: true)
            if !document.introduction.isEmpty {
                DesignedText(value: document.introduction, style: document.textStyles?["introduction"], root: document.appearance, muted: true)
            }
            DesignGrid(columns: document.appearance?.columns, mobileColumns: document.appearance?.mobileColumns,
                       gap: document.appearance?.gridGap ?? 32, columnGap: document.appearance?.gridColumnGap ?? 24, adaptive: true) {
                ForEach(document.categories.filter { $0.visible }) { category in
                    NavigationLink {
                        GalleryView(category: category, images: portfolio.images, document: document)
                    } label: {
                        DesignGrid(columns: category.layout?.columns, mobileColumns: category.layout?.mobileColumns, gap: CGFloat(category.layout?.gap ?? 12)) {
                            PortfolioImage(photo: category.images[category.hero], images: portfolio.images, root: document.appearance)
                            DesignedText(value: category.title, style: category.textStyles?["title"], root: document.appearance, size: 22, heading: true)
                            DesignedText(value: category.description, style: category.textStyles?["description"], root: document.appearance, muted: true)
                        }.modifier(BoxDesign(value: category.layout))
                    }.buttonStyle(.plain).accessibilityHint("Open category gallery")
                }
            }
        }.navigationTitle("Portfolio").navigationBarTitleDisplayMode(.inline)
    }
}
struct GalleryView: View {
    let category: PortfolioCategory
    let images: [String: UIImage]
    let document: PortfolioDocument
    var body: some View {
        PageSurface(root: document.appearance) {
            VStack(alignment: .leading, spacing: CGFloat(category.layout?.gap ?? 24)) {
                if category.textStyles?["title"] != nil {
                    DesignedText(value: category.title, style: category.textStyles?["title"], root: document.appearance, size: 34, heading: true)
                }
                DesignedText(value: category.description, style: category.textStyles?["description"], root: document.appearance, muted: true)
                DesignGrid(columns: category.layout?.columns ?? document.appearance?.columns ?? (document.galleryLayout == "single" ? 1 : nil),
                           mobileColumns: category.layout?.mobileColumns ?? document.appearance?.mobileColumns,
                           gap: category.layout?.gap.map { CGFloat($0) } ?? document.appearance?.gridGap ?? 32,
                           columnGap: category.layout?.gap.map { CGFloat($0) } ?? document.appearance?.gridColumnGap ?? 24, adaptive: true) {
                    ForEach(category.images.filter { $0.display?.hidden != true }) { photo in
                        NavigationLink {
                            PhotoDetail(photo: photo, images: images, document: document)
                        } label: {
                            VStack(alignment: .leading, spacing: 10) {
                                PortfolioImage(photo: photo, images: images, root: document.appearance)
                                if !photo.title.isEmpty {
                                    DesignedText(value: photo.title, style: photo.captionStyle, root: document.appearance)
                                }
                            }
                        }.buttonStyle(.plain).accessibilityHint("Open full photograph")
                            .layoutValue(key: PhotoSpan.self, value: photo.display?.span ?? 1)
                    }
                }
            }.modifier(BoxDesign(value: category.layout))
        }.navigationTitle(Text(verbatim: category.title))
    }
}
struct PhotoDetail: View {
    let photo: PortfolioPhoto
    let images: [String: UIImage]
    let document: PortfolioDocument
    @Environment(\.dismiss) private var dismiss
    @State private var scale: CGFloat = 1
    @State private var startingScale: CGFloat = 1
    private func setScale(_ value: CGFloat) { scale = min(5, max(1, value)); startingScale = scale }
    var body: some View {
        VStack(spacing: 12) {
            GeometryReader { geometry in
                ScrollView([.horizontal, .vertical]) {
                    PortfolioImage(photo: photo, images: images, root: document.appearance)
                        .frame(width: max(1, geometry.size.width) * scale,
                               height: max(1, geometry.size.height) * scale)
                        .gesture(MagnificationGesture()
                            .onChanged { scale = min(5, max(1, startingScale * $0)) }
                            .onEnded { _ in startingScale = scale })
                }
            }
            if !photo.title.isEmpty {
                DesignedText(value: photo.title, style: photo.captionStyle, root: document.appearance, defaultWeight: .semibold).padding(.horizontal)
            }
            HStack(spacing: 24) {
                Button { setScale(scale / 1.5) } label: { Label("Zoom out", systemImage: "minus.magnifyingglass") }.disabled(scale <= 1)
                Button("Reset zoom") { setScale(1) }
                Button { setScale(scale * 1.5) } label: { Label("Zoom in", systemImage: "plus.magnifyingglass") }.disabled(scale >= 5)
            }.labelStyle(.iconOnly).buttonStyle(.bordered).padding(.bottom)
        }.background(designColor(document.appearance?.palette?["background"], fallback: .white))
            .navigationTitle("Photograph").navigationBarTitleDisplayMode(.inline)
            .toolbar { Button { dismiss() } label: { Text(verbatim: document.labels?["viewerClose"] ?? "Close image") } }
    }
}
struct AboutView: View {
    let document: PortfolioDocument
    private var emailURL: URL? {
        let email = document.contact.email
        let pattern = #"^[A-Za-z0-9]+(?:[._+-][A-Za-z0-9]+)*@[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?:\.[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)+$"#
        guard !email.isEmpty, email.count <= 254,
              email.range(of: pattern, options: .regularExpression) != nil else { return nil }
        return URL(string: "mailto:" + email)
    }
    var body: some View {
        PageSurface(root: document.appearance, legacyWidth: 760) {
            DesignedText(value: document.labels?["aboutHeading"] ?? document.name, style: document.textStyles?["aboutHeading"], root: document.appearance, size: 34, heading: true)
            DesignedText(value: document.about, style: document.textStyles?["about"], root: document.appearance)
            if document.labels?["contactHeading"] != nil || document.textStyles?["contactHeading"] != nil {
                DesignedText(value: document.labels?["contactHeading"] ?? "Contact", style: document.textStyles?["contactHeading"], root: document.appearance, size: 22, heading: true)
            }
            if !document.contact.location.isEmpty && document.textStyles?["location"]?.hidden != true {
                Label { DesignedText(value: document.contact.location, style: document.textStyles?["location"], root: document.appearance) } icon: { Image(systemName: "mappin") }
            }
            if let emailURL, document.textStyles?["email"]?.hidden != true {
                Link(destination: emailURL) {
                    Label { DesignedText(value: document.contact.email, style: document.textStyles?["email"], root: document.appearance) } icon: { Image(systemName: "envelope") }
                }.accessibilityHint("Compose an email in your mail app")
            }
        }.navigationTitle("About")
    }
}
struct ResumeView: View {
    let document: PortfolioDocument
    var body: some View {
        PageSurface(root: document.appearance, legacyWidth: 760, legacySpacing: 28) {
            DesignedText(value: document.labels?["resumeHeading"] ?? document.name, style: document.textStyles?["resumeHeading"], root: document.appearance, size: 28, heading: true)
            if document.resume.isEmpty { Text("No resume sections have been added.") }
            ForEach(document.resume.indices, id: \.self) { index in
                let section = document.resume[index]
                if section.layout?.hidden != true {
                    VStack(alignment: .leading, spacing: CGFloat(section.layout?.gap ?? 20)) {
                        DesignedText(value: section.heading, style: section.headingStyle, root: document.appearance, size: 22, heading: true)
                        DesignGrid(columns: section.layout?.columns, mobileColumns: section.layout?.mobileColumns, gap: CGFloat(section.layout?.gap ?? 20)) {
                            ForEach(section.entries.indices, id: \.self) { entryIndex in
                                let entry = section.entries[entryIndex]
                                if entry.layout?.hidden != true {
                                    DesignGrid(columns: entry.layout?.columns, mobileColumns: entry.layout?.mobileColumns, gap: CGFloat(entry.layout?.gap ?? 6)) {
                                        DesignedText(value: entry.title, style: entry.textStyles?["title"], root: document.appearance, heading: true, defaultWeight: .semibold)
                                        DesignedText(value: entry.subtitle, style: entry.textStyles?["subtitle"], root: document.appearance)
                                        DesignedText(value: entry.dates, style: entry.textStyles?["dates"], root: document.appearance, size: 15, muted: true)
                                        DesignedText(value: entry.description, style: entry.textStyles?["description"], root: document.appearance)
                                    }.modifier(BoxDesign(value: entry.layout))
                                }
                            }
                        }
                    }.modifier(BoxDesign(value: section.layout))
                }
            }
        }.navigationTitle("Resume")
    }
}
'''

_INFO_PLIST = '''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleDevelopmentRegion</key><string>en</string>
<key>CFBundleDisplayName</key><string>Portfolio</string>
<key>CFBundleExecutable</key><string>$(EXECUTABLE_NAME)</string>
<key>CFBundleIdentifier</key><string>$(PRODUCT_BUNDLE_IDENTIFIER)</string>
<key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
<key>CFBundleName</key><string>$(PRODUCT_NAME)</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>1.0</string>
<key>CFBundleVersion</key><string>1</string>
<key>LSRequiresIPhoneOS</key><true/>
<key>UIApplicationSceneManifest</key><dict><key>UIApplicationSupportsMultipleScenes</key><true/></dict>
<key>UILaunchScreen</key><dict/>
<key>UISupportedInterfaceOrientations</key><array>
<string>UIInterfaceOrientationPortrait</string><string>UIInterfaceOrientationLandscapeLeft</string><string>UIInterfaceOrientationLandscapeRight</string>
</array>
<key>UISupportedInterfaceOrientations~ipad</key><array>
<string>UIInterfaceOrientationPortrait</string><string>UIInterfaceOrientationPortraitUpsideDown</string>
<string>UIInterfaceOrientationLandscapeLeft</string><string>UIInterfaceOrientationLandscapeRight</string>
</array>
</dict></plist>
'''

_PROJECT = '''// !$*UTF8*$!
{
    archiveVersion = 1;
    classes = {};
    objectVersion = 56;
    objects = {
        A00000000000000000000001 = {isa = PBXBuildFile; fileRef = A00000000000000000000011; };
        A00000000000000000000002 = {isa = PBXBuildFile; fileRef = A00000000000000000000012; };
        A00000000000000000000011 = {isa = PBXFileReference; lastKnownFileType = sourcecode.swift; path = PortfolioApp.swift; sourceTree = "<group>"; };
        A00000000000000000000012 = {isa = PBXFileReference; lastKnownFileType = folder; path = PortfolioContent; sourceTree = "<group>"; };
        A00000000000000000000013 = {isa = PBXFileReference; lastKnownFileType = text.plist.xml; path = Info.plist; sourceTree = "<group>"; };
        A00000000000000000000014 = {isa = PBXFileReference; explicitFileType = wrapper.application; includeInIndex = 0; path = Portfolio.app; sourceTree = BUILT_PRODUCTS_DIR; };
        A00000000000000000000020 = {isa = PBXGroup; children = (A00000000000000000000021, A00000000000000000000022); sourceTree = "<group>"; };
        A00000000000000000000021 = {isa = PBXGroup; children = (A00000000000000000000011, A00000000000000000000012, A00000000000000000000013); path = Portfolio; sourceTree = "<group>"; };
        A00000000000000000000022 = {isa = PBXGroup; children = (A00000000000000000000014); name = Products; sourceTree = "<group>"; };
        A00000000000000000000030 = {isa = PBXSourcesBuildPhase; buildActionMask = 2147483647; files = (A00000000000000000000001); runOnlyForDeploymentPostprocessing = 0; };
        A00000000000000000000031 = {isa = PBXResourcesBuildPhase; buildActionMask = 2147483647; files = (A00000000000000000000002); runOnlyForDeploymentPostprocessing = 0; };
        A00000000000000000000032 = {isa = PBXFrameworksBuildPhase; buildActionMask = 2147483647; files = (); runOnlyForDeploymentPostprocessing = 0; };
        A00000000000000000000040 = {
            isa = PBXNativeTarget;
            buildConfigurationList = A00000000000000000000051;
            buildPhases = (A00000000000000000000030, A00000000000000000000032, A00000000000000000000031);
            buildRules = (); dependencies = (); name = Portfolio;
            productName = Portfolio; productReference = A00000000000000000000014;
            productType = "com.apple.product-type.application";
        };
        A00000000000000000000041 = {
            isa = PBXProject;
            attributes = {BuildIndependentTargetsInParallel = YES; LastSwiftUpdateCheck = 1500; LastUpgradeCheck = 1500; };
            buildConfigurationList = A00000000000000000000050;
            compatibilityVersion = "Xcode 14.0";
            developmentRegion = en; hasScannedForEncodings = 0; knownRegions = (en, Base);
            mainGroup = A00000000000000000000020; productRefGroup = A00000000000000000000022;
            projectDirPath = ""; projectRoot = ""; targets = (A00000000000000000000040);
        };
        A00000000000000000000050 = {isa = XCConfigurationList; buildConfigurations = (A00000000000000000000060, A00000000000000000000061); defaultConfigurationIsVisible = 0; defaultConfigurationName = Release; };
        A00000000000000000000051 = {isa = XCConfigurationList; buildConfigurations = (A00000000000000000000062, A00000000000000000000063); defaultConfigurationIsVisible = 0; defaultConfigurationName = Release; };
        A00000000000000000000060 = {
            isa = XCBuildConfiguration;
            buildSettings = {CLANG_ENABLE_MODULES = YES; CLANG_ENABLE_OBJC_ARC = YES; SDKROOT = iphoneos; IPHONEOS_DEPLOYMENT_TARGET = 17.0; DEBUG_INFORMATION_FORMAT = dwarf; SWIFT_OPTIMIZATION_LEVEL = "-Onone"; SWIFT_ACTIVE_COMPILATION_CONDITIONS = DEBUG; ONLY_ACTIVE_ARCH = YES; };
            name = Debug;
        };
        A00000000000000000000061 = {
            isa = XCBuildConfiguration;
            buildSettings = {CLANG_ENABLE_MODULES = YES; CLANG_ENABLE_OBJC_ARC = YES; SDKROOT = iphoneos; IPHONEOS_DEPLOYMENT_TARGET = 17.0; DEBUG_INFORMATION_FORMAT = "dwarf-with-dsym"; SWIFT_OPTIMIZATION_LEVEL = "-O"; SWIFT_COMPILATION_MODE = wholemodule; };
            name = Release;
        };
        A00000000000000000000062 = {
            isa = XCBuildConfiguration;
            buildSettings = {CODE_SIGN_STYLE = Automatic; CURRENT_PROJECT_VERSION = 1; GENERATE_INFOPLIST_FILE = NO; INFOPLIST_FILE = Portfolio/Info.plist; IPHONEOS_DEPLOYMENT_TARGET = 17.0; LD_RUNPATH_SEARCH_PATHS = ("$(inherited)", "@executable_path/Frameworks"); MARKETING_VERSION = 1.0; PRODUCT_BUNDLE_IDENTIFIER = com.example.portfolio; PRODUCT_NAME = "$(TARGET_NAME)"; SUPPORTED_PLATFORMS = "iphoneos iphonesimulator"; SUPPORTS_MACCATALYST = NO; SWIFT_EMIT_LOC_STRINGS = YES; SWIFT_VERSION = 5.0; TARGETED_DEVICE_FAMILY = "1,2"; };
            name = Debug;
        };
        A00000000000000000000063 = {
            isa = XCBuildConfiguration;
            buildSettings = {CODE_SIGN_STYLE = Automatic; CURRENT_PROJECT_VERSION = 1; GENERATE_INFOPLIST_FILE = NO; INFOPLIST_FILE = Portfolio/Info.plist; IPHONEOS_DEPLOYMENT_TARGET = 17.0; LD_RUNPATH_SEARCH_PATHS = ("$(inherited)", "@executable_path/Frameworks"); MARKETING_VERSION = 1.0; PRODUCT_BUNDLE_IDENTIFIER = com.example.portfolio; PRODUCT_NAME = "$(TARGET_NAME)"; SUPPORTED_PLATFORMS = "iphoneos iphonesimulator"; SUPPORTS_MACCATALYST = NO; SWIFT_EMIT_LOC_STRINGS = YES; SWIFT_VERSION = 5.0; TARGETED_DEVICE_FAMILY = "1,2"; };
            name = Release;
        };
    };
    rootObject = A00000000000000000000041;
}
'''

_SCHEME = '''<?xml version="1.0" encoding="UTF-8"?>
<Scheme LastUpgradeVersion="1500" version="1.3">
  <BuildAction parallelizeBuildables="YES" buildImplicitDependencies="YES">
    <BuildActionEntries><BuildActionEntry buildForTesting="YES" buildForRunning="YES" buildForProfiling="YES" buildForArchiving="YES" buildForAnalyzing="YES">
      <BuildableReference BuildableIdentifier="primary" BlueprintIdentifier="A00000000000000000000040" BuildableName="Portfolio.app" BlueprintName="Portfolio" ReferencedContainer="container:Portfolio.xcodeproj"/>
    </BuildActionEntry></BuildActionEntries>
  </BuildAction>
  <TestAction buildConfiguration="Debug" selectedDebuggerIdentifier="Xcode.DebuggerFoundation.Debugger.LLDB" selectedLauncherIdentifier="Xcode.IDEFoundation.Launcher.LLDB" shouldUseLaunchSchemeArgsEnv="YES"><Testables/></TestAction>
  <LaunchAction buildConfiguration="Debug" selectedDebuggerIdentifier="Xcode.DebuggerFoundation.Debugger.LLDB" selectedLauncherIdentifier="Xcode.IDEFoundation.Launcher.LLDB" launchStyle="0" useCustomWorkingDirectory="NO" ignoresPersistentStateOnLaunch="NO" debugDocumentVersioning="YES" debugServiceExtension="internal" allowLocationSimulation="YES">
    <BuildableProductRunnable runnableDebuggingMode="0"><BuildableReference BuildableIdentifier="primary" BlueprintIdentifier="A00000000000000000000040" BuildableName="Portfolio.app" BlueprintName="Portfolio" ReferencedContainer="container:Portfolio.xcodeproj"/></BuildableProductRunnable>
  </LaunchAction>
  <ProfileAction buildConfiguration="Release" shouldUseLaunchSchemeArgsEnv="YES" savedToolIdentifier="" useCustomWorkingDirectory="NO" debugDocumentVersioning="YES">
    <BuildableProductRunnable runnableDebuggingMode="0"><BuildableReference BuildableIdentifier="primary" BlueprintIdentifier="A00000000000000000000040" BuildableName="Portfolio.app" BlueprintName="Portfolio" ReferencedContainer="container:Portfolio.xcodeproj"/></BuildableProductRunnable>
  </ProfileAction>
  <AnalyzeAction buildConfiguration="Debug"/>
  <ArchiveAction buildConfiguration="Release" revealArchiveInOrganizer="YES"/>
</Scheme>
'''

_README = '''# Native portfolio for iPhone and iPad

This is a standalone SwiftUI application for iOS 17 and later, using Swift 5.
Open Portfolio.xcodeproj in Xcode 15 or later on a Mac with an iOS SDK.
It uses only Apple frameworks: no packages, web views, network content,
accounts, secrets, Studio service, xcodegen or package fetch is required.

## Build for the simulator

From the extracted directory, run:

```sh
xcodebuild -project Portfolio.xcodeproj -scheme Portfolio -sdk iphonesimulator -configuration Debug CODE_SIGNING_ALLOWED=NO build
```

No signing credentials are needed for that simulator build. Select a local
simulator in Xcode to run. Producing this archive is not evidence of a successful
compile, simulator run, device installation or TestFlight distribution.

## Signing and distribution

Before signing, installing on a physical device or distributing, choose your
own bundle identifier (the default is com.example.portfolio) and development
team in the Portfolio target's Signing & Capabilities settings. Configure your
own Apple signing assets and review distribution requirements separately.

## Content and photographs

Portfolio/PortfolioContent/portfolio.json contains the shared leaf.portfolio.v1 data.
All prose is decoded as data and rendered as literal text. Only the validated
contact email opens a native mailto link. The app does not download anything.
Portfolio/PortfolioContent/images contains optimized JPEG previews, not the original
photo files. PortfolioContent is a folder reference in the resource build phase, so
its JSON and images retain their directory structure inside the app bundle.
Keep that folder reference when editing the Xcode project.

The Portfolio tab opens category galleries and full photographs. Pinch or use
the named zoom buttons, then scroll to inspect details; Reset zoom restores
aspect-fit viewing. About provides contact details and Resume shows the shared
sections. Missing or unreadable bundle content produces a readable error.

source-manifest.json records the saved revision, SHA256 of canonical JSON
(UTF-8, sorted keys, compact separators, without the file's trailing newline),
each material's original sha256 and preview_sha256, and SHA256 and byte length
of every other archive file. The manifest excludes itself to avoid a circular
digest. Preview file hashes must match preview_sha256. ZIP entries have fixed
timestamps, permissions and ordering; no local paths or author metadata enter
the archive. Editing exported content invalidates the corresponding hashes.
'''
