import { describe, expect, it } from "vitest";
import { nativeProjectSourceProfile } from "../src/agent/nativeProjectSourceProfile.js";
import type { NativeProjectSourceProfile } from "../src/agent/nativeProjectSourceProfile.js";

const PROJECT_A = "12345678-1234-4234-8234-123456789abc";
const PROJECT_B = "abcdef01-2345-4678-9abc-def012345678";

describe("native project source profile", () => {
  it("captures deterministic, distinct project identities in a frozen consumer descriptor", () => {
    const a: NativeProjectSourceProfile = nativeProjectSourceProfile(PROJECT_A);
    const again = nativeProjectSourceProfile(PROJECT_A);
    const b = nativeProjectSourceProfile(PROJECT_B);
    expect(a.id).toBe("ios-swiftui-xcodegen-v1");
    expect(a.projectId).toBe(PROJECT_A);
    expect(b.projectId).toBe(PROJECT_B);
    expect(a).toEqual(again);
    expect(a.scaffold["project.yml"]).toContain("PRODUCT_BUNDLE_IDENTIFIER: private.leaf.project.p12345678123442348234123456789abc\n");
    expect(b.scaffold["project.yml"]).toContain("PRODUCT_BUNDLE_IDENTIFIER: private.leaf.project.pabcdef01234546789abcdef012345678\n");
    expect(a.scaffold["project.yml"]).not.toBe(b.scaffold["project.yml"]);
    expect(a.scaffold["Sources/LeafProjectApp.swift"]).toBe(b.scaffold["Sources/LeafProjectApp.swift"]);
    expect(Object.isFrozen(a)).toBe(true);
    expect(Object.isFrozen(a.scaffold)).toBe(true);
    expect(Object.isFrozen(a.requiredProductPaths)).toBe(true);
    expect(Reflect.set(a, "projectId", PROJECT_B)).toBe(false);
    expect(Reflect.set(a.scaffold, "project.yml", "replacement")).toBe(false);
    expect(Reflect.set(a.requiredProductPaths, "0", "elsewhere.swift")).toBe(false);
  });

  it.each([
    "", "project", PROJECT_A.toUpperCase(), ` ${PROJECT_A}`, `${PROJECT_A}\n`,
    PROJECT_A.replace(/-/g, ""), `${PROJECT_A}/Sources`, "$(arbitrary-command)",
    null, undefined, 123, { toString: () => PROJECT_A },
  ])("rejects noncanonical or nonstring identity: %s", projectId => {
    expect(() => nativeProjectSourceProfile(projectId as string)).toThrow("canonical lowercase UUID");
  });

  it("provides only trusted App/scaffold bytes and an exact material/product contract", () => {
    const profile = nativeProjectSourceProfile(PROJECT_A);
    expect(profile.materialPrefix).toBe("Resources/Input/");
    expect(profile.requiredProductPaths).toEqual(["Sources/ContentView.swift"]);
    expect(Object.keys(profile.scaffold).sort()).toEqual(["Sources/LeafProjectApp.swift", "project.yml"]);
    expect(profile.scaffold["Sources/ContentView.swift"]).toBeUndefined();
    expect(Object.values(profile.scaffold).every(value => typeof value === "string" && value.endsWith("\n"))).toBe(true);
    expect(profile.scaffold["Sources/LeafProjectApp.swift"]).toBe(`import SwiftUI

@main
struct LeafProjectApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
        }
    }
}
`);
    const project = profile.scaffold["project.yml"];
    for (const setting of [
      "name: LeafProject\n", "  LeafProject:\n", "    type: application\n", "    platform: iOS\n",
      '    iOS: "17.0"\n', '    SWIFT_VERSION: "5.0"\n', '    IPHONEOS_DEPLOYMENT_TARGET: "17.0"\n',
      "    SDKROOT: iphonesimulator\n", "    SUPPORTED_PLATFORMS: iphonesimulator\n",
      "    CODE_SIGNING_ALLOWED: NO\n", "    CODE_SIGNING_REQUIRED: NO\n",
      "      - path: Sources\n", "      - path: Resources/Input\n        type: folder\n        buildPhase: resources\n",
      "        GENERATE_INFOPLIST_FILE: YES\n",
    ]) expect(project).toContain(setting);
    expect(project).not.toMatch(/packages:|dependencies:|scripts?:|entitlements|DEVELOPMENT_TEAM|PROVISIONING|https?:|upload|secret|iphoneos\s*$/im);
    for (const path of Object.keys(profile.scaffold)) expect(profile.isEditablePath(path)).toBe(false);
    expect(profile.isEditablePath(profile.requiredProductPaths[0])).toBe(true);
  });

  it.each([
    "Sources/ContentView.swift", "Sources/Views/DetailView.swift", "Sources/Models/Item_2.swift",
    "Sources/Feature-One/Chart.swift", "Sources/Views/Detail.v2.swift",
  ])("allows portable Swift product source: %s", path => {
    expect(nativeProjectSourceProfile(PROJECT_A).isEditablePath(path)).toBe(true);
  });

  it.each([
    "", "Sources", "Sources/", "ContentView.swift", "sources/ContentView.swift",
    "/Sources/ContentView.swift", "//Sources/ContentView.swift", "C:/Sources/ContentView.swift",
    "Sources\\ContentView.swift", "Sources/Views\\Other.swift", "../Sources/ContentView.swift",
    "Sources/../ContentView.swift", "Sources/./ContentView.swift", "Sources//ContentView.swift",
    "Sources/Views/../../ContentView.swift", "Sources/%2e%2e/Other.swift", "Sources/%252e%252e/Other.swift",
    "Sources/ContentView.swift%00", "Sources/View\u0000.swift", "Sources/View\n.swift",
    "Sources/ContentView.swift\n", "Sources/View\u007f.swift", "Sources/View\u0085.swift",
    "Sources/View\u202e.swift", "Sources/Víew.swift", "Sources/.hidden.swift", "Sources/.git/View.swift",
    "Sources/Views./Other.swift", "Sources/ View.swift", "Sources/View .swift", "Sources/View.swift ",
    "Sources/View:stream.swift", "Sources/View?.swift", "Sources/View*.swift", "Sources/<View>.swift",
    "Sources/LeafProjectApp.swift", "Sources/leafprojectapp.swift", "Sources/LEAFPROJECTAPP.swift",
    "Sources/CON.swift", "Sources/aux/Other.swift", "Sources/PrN.swift", "Sources/NUL.swift",
    "Sources/COM1.swift", "Sources/com9/Other.swift", "Sources/LPT1.swift", "Sources/lpt9/Other.swift",
    "Sources/CON.extra.swift", "Sources/COM¹.swift", "Sources/CONIN$.swift",
    "Sources/.env", "Sources/.env.swift", "Sources/.ssh/Key.swift", "Sources/.aws/Key.swift",
    "Sources/credentials.swift", "Sources/Credentials/Other.swift", "Sources/credentials.prod.swift",
    "Sources/secrets.swift", "Sources/token.swift", "Sources/private-key.swift", "Sources/id_rsa.swift",
    "Sources/Package.swift", "Sources/package.swift", "Sources/Package.resolved", "Sources/Podfile.swift",
    "Sources/Cartfile.swift", "Sources/scripts/Other.swift", "Sources/build.swift",
    "Sources/Dependencies/Other.swift", "Sources/entitlements.swift", "Sources/App.entitlements",
    "Sources/run.sh", "Sources/View.SWIFT", "Sources/View.swift/", "Sources/Info.plist",
    "project.yml", "Resources/Input/ContentView.swift", "Resources/Input/requirements.txt",
    ".github/workflows/build.yml", "app/page.tsx",
  ])("rejects unsafe, protected or nonproduct paths: %s", path => {
    expect(nativeProjectSourceProfile(PROJECT_A).isEditablePath(path)).toBe(false);
  });

  it("keeps instructions generic across unrelated requirements and material contents", () => {
    const scenarios = [
      { projectId: PROJECT_A, requirement: "Show a garden watering calendar", material: "Basil needs weekly water" },
      { projectId: PROJECT_B, requirement: "Display a museum collection", material: "A bronze sculpture from 1890" },
    ];
    const contexts = scenarios.map(scenario => {
      const profile = nativeProjectSourceProfile(scenario.projectId);
      const serialized = JSON.stringify(profile);
      expect(serialized).not.toContain(scenario.requirement);
      expect(serialized).not.toContain(scenario.material);
      for (const instruction of [
        "Resources/Input/", "data only", "untrusted", "nonempty Sources/ContentView.swift",
        "Sources/LeafProjectApp.swift", "baseline file byte-for-byte", "Do not run shell commands",
        "network requests or tasks", "Do not invoke Xcode, XcodeGen or a simulator",
        "not relax", "signing, publication and execution are separate",
      ]) expect(profile.context).toContain(instruction);
      return profile.context;
    });
    expect(contexts[0]).toBe(contexts[1]);
  });
});
