using System;

namespace LeafSolarDesign.ScriptHost
{
    public enum ToolDecision
    {
        Accept,
        Malformed,
        UnknownKey,
        SignatureInvalid,
        DigestMismatch,
        IdentityMismatch,
        Revoked,
        StaleRevocationList,
        Unreviewed,
        Unentitled,
        HostApiTooLow,
        CapabilityViolation,
        Timeout,
        CompileFailed,
        ScriptError,
        RevocationInvalid,
        WrongTenant,
        ConfirmationDeclined
    }

    public static class ToolDecisions
    {
        public static string Code(ToolDecision decision)
        {
            switch (decision)
            {
                case ToolDecision.Accept: return "accept";
                case ToolDecision.Malformed: return "malformed";
                case ToolDecision.UnknownKey: return "unknown_key";
                case ToolDecision.SignatureInvalid: return "signature_invalid";
                case ToolDecision.DigestMismatch: return "digest_mismatch";
                case ToolDecision.IdentityMismatch: return "identity_mismatch";
                case ToolDecision.Revoked: return "revoked";
                case ToolDecision.StaleRevocationList: return "stale_revocation_list";
                case ToolDecision.Unreviewed: return "unreviewed";
                case ToolDecision.Unentitled: return "unentitled";
                case ToolDecision.HostApiTooLow: return "host_api_too_low";
                case ToolDecision.CapabilityViolation: return "capability_violation";
                case ToolDecision.Timeout: return "timeout";
                case ToolDecision.CompileFailed: return "compile_failed";
                case ToolDecision.ScriptError: return "script_error";
                case ToolDecision.RevocationInvalid: return "revocation_invalid";
                case ToolDecision.WrongTenant: return "wrong_tenant";
                case ToolDecision.ConfirmationDeclined: return "confirmation_declined";
                default: throw new ArgumentOutOfRangeException(nameof(decision), decision, "Unknown tool decision.");
            }
        }
    }
}
