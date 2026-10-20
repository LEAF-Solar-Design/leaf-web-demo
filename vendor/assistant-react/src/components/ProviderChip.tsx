import type { ThreadV1 } from "../../../assistant-contract/src/index";
import { color, figure, meta, radius } from "../tokens";

export interface ProviderChipProps {
  provider: ThreadV1["provider"];
  scope: ThreadV1["scope"];
}

/** Who answers (provider and model) and whose data it can see (the scope label). */
export function ProviderChip({ provider, scope }: ProviderChipProps) {
  return (
    <div data-testid="assistant-provider" style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
      <span
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
          padding: "2px 10px",
          border: `1px solid ${color.border}`,
          borderRadius: radius,
          background: color.secondary,
          color: color["secondary-foreground"],
          fontSize: 13,
        }}
      >
        <span>{provider.label}</span>
        <span style={{ ...figure, color: color["muted-foreground"] }}>{provider.model}</span>
      </span>
      <span style={meta}>{scope.label}</span>
    </div>
  );
}
