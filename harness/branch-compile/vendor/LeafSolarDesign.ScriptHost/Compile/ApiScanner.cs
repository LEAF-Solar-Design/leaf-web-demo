using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using Microsoft.CodeAnalysis.Operations;

namespace LeafSolarDesign.ScriptHost.Compile
{
    /// <summary>Static capability policy plus trust, not a sandbox. Runtime mediation remains necessary.</summary>
    public static class ApiScanner
    {
        private static readonly HashSet<string> PathSetters = new HashSet<string>(StringComparer.Ordinal)
        {
            "Autodesk.AutoCAD.DatabaseServices.TextStyleTableRecord.FileName",
            "Autodesk.AutoCAD.DatabaseServices.TextStyleTableRecord.BigFontFileName",
            "Autodesk.AutoCAD.DatabaseServices.BlockTableRecord.PathName",
            "Autodesk.AutoCAD.DatabaseServices.RasterImageDef.SourceFileName",
            "Autodesk.AutoCAD.DatabaseServices.UnderlayDefinition.SourceFileName"
        };

        public static IReadOnlyList<string> Scan(Compilation compilation, IReadOnlyList<string> capabilities,
            CancellationToken cancellationToken = default)
            => Scan(compilation, capabilities, ApiAllowlist.Policy, cancellationToken);

        internal static IReadOnlyList<string> Scan(Compilation compilation, IReadOnlyList<string> capabilities,
            ApiPolicy policy, CancellationToken cancellationToken = default)
        {
            if (policy.Error != null) return new[] { "scan: policy unavailable: " + policy.Error };
            var state = new ScanState(compilation, capabilities, policy);
            var findings = new SortedSet<string>(StringComparer.Ordinal);
            bool files = capabilities.Any(c => c.StartsWith("files.read:", StringComparison.Ordinal) || c.StartsWith("files.write:", StringComparison.Ordinal));
            bool http = capabilities.Any(c => c.StartsWith("net.http:", StringComparison.Ordinal));
            foreach (var tree in compilation.SyntaxTrees)
            {
                var model = compilation.GetSemanticModel(tree);
                var root = tree.GetRoot(cancellationToken);
                foreach (var token in root.DescendantTokens())
                {
                    cancellationToken.ThrowIfCancellationRequested();
                    if (token.IsKind(SyntaxKind.UnsafeKeyword) || token.IsKind(SyntaxKind.FixedKeyword) ||
                        token.IsKind(SyntaxKind.StackAllocKeyword) || token.IsKind(SyntaxKind.ExternKeyword))
                        Add(findings, token.GetLocation(), "forbidden syntax", token.ValueText);
                }
                foreach (var node in root.DescendantNodesAndSelf())
                {
                    cancellationToken.ThrowIfCancellationRequested();
                    if (node is PointerTypeSyntax) Add(findings, node.GetLocation(), "pointer type", node.ToString());
                    if (node is DestructorDeclarationSyntax) Add(findings, node.GetLocation(), "forbidden syntax", "finalizer");
                    if (node is AssignmentExpressionSyntax assignment &&
                        (assignment.IsKind(SyntaxKind.AddAssignmentExpression) || assignment.IsKind(SyntaxKind.SubtractAssignmentExpression)) &&
                        model.GetSymbolInfo(assignment.Left, cancellationToken).Symbol is IEventSymbol eventSymbol)
                        Add(findings, assignment.GetLocation(), "event subscription", eventSymbol.ToDisplayString());
                    void CheckImplicit(ISymbol symbol) => state.Check(symbol, node.GetLocation(), findings, files, http);
                    if (node is CommonForEachStatementSyntax each)
                    {
                        var iteration = model.GetForEachStatementInfo(each);
                        CheckImplicit(iteration.GetEnumeratorMethod);
                        CheckImplicit(iteration.MoveNextMethod);
                        CheckImplicit(iteration.CurrentProperty);
                        CheckImplicit(iteration.DisposeMethod);
                        if (each is ForEachVariableStatementSyntax variable)
                            CheckDeconstruction(model.GetDeconstructionInfo(variable), CheckImplicit);
                    }
                    if (node is AwaitExpressionSyntax awaitExpression)
                    {
                        var awaitInfo = model.GetAwaitExpressionInfo(awaitExpression);
                        CheckImplicit(awaitInfo.GetAwaiterMethod);
                        CheckImplicit(awaitInfo.IsCompletedProperty);
                        CheckImplicit(awaitInfo.GetResultMethod);
                    }
                    if (node is UsingStatementSyntax usingStatement)
                    {
                        if (usingStatement.Expression != null)
                            CheckDispose(model.GetTypeInfo(usingStatement.Expression, cancellationToken).Type, compilation, CheckImplicit);
                        if (usingStatement.Declaration != null)
                            foreach (var variable in usingStatement.Declaration.Variables)
                                CheckDispose((model.GetDeclaredSymbol(variable, cancellationToken) as ILocalSymbol)?.Type, compilation, CheckImplicit);
                    }
                    if (node is LocalDeclarationStatementSyntax declaration && !declaration.UsingKeyword.IsKind(SyntaxKind.None))
                        foreach (var variable in declaration.Declaration.Variables)
                            CheckDispose((model.GetDeclaredSymbol(variable, cancellationToken) as ILocalSymbol)?.Type, compilation, CheckImplicit);
                    if (node is AssignmentExpressionSyntax deconstruction && deconstruction.IsKind(SyntaxKind.SimpleAssignmentExpression))
                        CheckDeconstruction(model.GetDeconstructionInfo(deconstruction), CheckImplicit);
                    if (node is ExpressionSyntax expression)
                    {
                        var conversion = model.GetConversion(expression, cancellationToken);
                        if (conversion.IsUserDefined) CheckImplicit(conversion.MethodSymbol);
                        if (expression.Parent is InitializerExpressionSyntax initializer && initializer.IsKind(SyntaxKind.CollectionInitializerExpression))
                        {
                            var add = model.GetCollectionInitializerSymbolInfo(expression, cancellationToken);
                            CheckImplicit(add.Symbol);
                            foreach (var candidate in add.CandidateSymbols) CheckImplicit(candidate);
                        }
                    }
                    if (!(node is ExpressionSyntax) && !(node is AttributeSyntax)) continue;
                    var info = model.GetSymbolInfo(node, cancellationToken);
                    if (node is ExpressionSyntax && info.Symbol is IPropertySymbol property)
                    {
                        var member = state.Canonical(property.OriginalDefinition);
                        if (PathSetters.Contains(member) && IsPropertyWrite(model.GetOperation(node, cancellationToken)))
                            Add(findings, node.GetLocation(), "path setter denied", member);
                    }
                    if (info.Symbol == null || info.Symbol is IErrorTypeSymbol)
                    {
                        // A restricted reference set may omit a denied API's assembly. Resolve
                        // its namespace prefix semantically, never by searching source text.
                        string name = NamespaceQualifiedName(node, model, cancellationToken);
                        if (name != null && state.DeniedNamespaceName(name))
                            Add(findings, node.GetLocation(), "denied namespace", name);
                        if (name != null && policy.DeniedTypes.Contains(name))
                            Add(findings, node.GetLocation(), "denied type", name);
                        if (IsHostName(name) && !ApiAllowlist.ToolFacingTypes.Contains(name))
                            Add(findings, node.GetLocation(), "host internal", name);
                    }
                    // Namespace qualifiers are checked through the complete reference. Otherwise
                    // even a fully qualified tool-facing type would be rejected at its prefix.
                    if (info.Symbol is INamespaceSymbol namespaceSymbol && IsHostName(namespaceSymbol.ToDisplayString()) &&
                        (node.Parent is QualifiedNameSyntax || node.Parent is AliasQualifiedNameSyntax ||
                         node.Parent is MemberAccessExpressionSyntax)) continue;
                    state.Check(info.Symbol, node.GetLocation(), findings, files, http);
                    foreach (var candidate in info.CandidateSymbols) state.Check(candidate, node.GetLocation(), findings, files, http);
                    if (node is ExpressionSyntax)
                    {
                        var type = model.GetTypeInfo(node, cancellationToken);
                        state.Check(type.Type, node.GetLocation(), findings, files, http);
                        state.Check(type.ConvertedType, node.GetLocation(), findings, files, http);
                    }
                }
            }
            return findings.ToArray();
        }

        private static bool IsPropertyWrite(IOperation operation)
        {
            if (!(operation is IPropertyReferenceOperation)) return false;
            for (;;)
            {
                var parent = operation.Parent;
                if (parent is IConversionOperation conversion && conversion.Operand == operation ||
                    parent is IParenthesizedOperation parentheses && parentheses.Operand == operation ||
                    parent is ITupleOperation)
                {
                    operation = parent;
                    continue;
                }
                if (parent is IAssignmentOperation assignment) return assignment.Target == operation;
                return parent is IIncrementOrDecrementOperation increment && increment.Target == operation;
            }
        }

        private static void CheckDeconstruction(DeconstructionInfo info, Action<ISymbol> check)
        {
            check(info.Method);
            if (!info.Nested.IsDefault)
                foreach (var nested in info.Nested) CheckDeconstruction(nested, check);
        }

        private static void CheckDispose(ITypeSymbol type, Compilation compilation, Action<ISymbol> check)
        {
            var disposable = compilation.GetTypeByMetadataName("System.IDisposable");
            var dispose = disposable?.GetMembers("Dispose").OfType<IMethodSymbol>().FirstOrDefault();
            if (type == null || dispose == null) return;
            var implementation = type.FindImplementationForInterfaceMember(dispose);
            if (implementation != null) check(implementation);
            else if (SymbolEqualityComparer.Default.Equals(type, disposable) || type.AllInterfaces.Contains(disposable, SymbolEqualityComparer.Default))
                check(dispose);
            else
                foreach (var method in type.GetMembers("Dispose").OfType<IMethodSymbol>())
                    if (!method.IsStatic && method.Parameters.Length == 0) check(method);
        }

        private static string NamespaceQualifiedName(SyntaxNode node, SemanticModel model, CancellationToken token)
        {
            var symbol = model.GetSymbolInfo(node, token).Symbol;
            if (symbol is IAliasSymbol alias) symbol = alias.Target;
            if (symbol is INamespaceSymbol ns) return ns.ToDisplayString();
            if (symbol != null && !(symbol is IErrorTypeSymbol)) return null;
            SyntaxNode left;
            SimpleNameSyntax right;
            if (node is QualifiedNameSyntax qualified) { left = qualified.Left; right = qualified.Right; }
            else if (node is MemberAccessExpressionSyntax member) { left = member.Expression; right = member.Name; }
            else return null;
            string prefix = NamespaceQualifiedName(left, model, token);
            return prefix == null ? null : prefix + "." + right.Identifier.ValueText;
        }

        private sealed class ScanState
        {
            private readonly Compilation compilation;
            private readonly IReadOnlyList<string> capabilities;
            private readonly ApiPolicy policy;
            // Canonical names are cached per symbol for this Scan call only.
            private readonly Dictionary<ISymbol, string> names = new Dictionary<ISymbol, string>(SymbolEqualityComparer.Default);

            internal ScanState(Compilation compilation, IReadOnlyList<string> capabilities, ApiPolicy policy)
            { this.compilation = compilation; this.capabilities = capabilities; this.policy = policy; }

            internal string Canonical(ISymbol symbol)
            {
                if (names.TryGetValue(symbol, out var name)) return name;
                ISymbol definition = symbol is IMethodSymbol method && method.ReducedFrom != null ? method.ReducedFrom : symbol;
                definition = definition.OriginalDefinition;
                if (definition is INamedTypeSymbol type)
                    name = (type.ContainingType != null ? Canonical(type.ContainingType) + "." :
                        type.ContainingNamespace == null || type.ContainingNamespace.IsGlobalNamespace ? "" :
                        type.ContainingNamespace.ToDisplayString() + ".") + type.MetadataName;
                else if (definition.ContainingType != null)
                    name = Canonical(definition.ContainingType) + "." + definition.Name;
                else name = definition.ToDisplayString();
                names[symbol] = name;
                return name;
            }

            private bool FullType(INamedTypeSymbol type) => type != null &&
                (policy.AllowedTypes.Contains(Canonical(type)) ||
                 policy.AllowedNamespaces.Contains(type.ContainingNamespace?.ToDisplayString() ?? ""));

            private bool Exempt(ISymbol symbol) => symbol.IsImplicitlyDeclared ||
                symbol.Locations.Any(l => l.IsInSource && compilation.ContainsSyntaxTree(l.SourceTree)) ||
                symbol is ITypeParameterSymbol || symbol is INamespaceSymbol || symbol is IErrorTypeSymbol ||
                symbol is ILocalSymbol || symbol is IParameterSymbol || symbol is IRangeVariableSymbol ||
                symbol is ILabelSymbol || symbol is IDiscardSymbol ||
                (symbol is INamedTypeSymbol type && type.IsAnonymousType) ||
                (symbol is IFieldSymbol field && field.ContainingType?.IsTupleType == true);

            private bool DeniedNamespace(INamespaceSymbol ns)
            {
                // Walk namespace ancestors against the policy HashSet, never scan the denied list.
                for (; ns != null && !ns.IsGlobalNamespace; ns = ns.ContainingNamespace)
                    if (policy.DeniedNamespaces.Contains(ns.ToDisplayString())) return true;
                return false;
            }

            internal bool DeniedNamespaceName(string name)
            {
                for (;;)
                {
                    if (policy.DeniedNamespaces.Contains(name)) return true;
                    int dot = name.LastIndexOf('.');
                    if (dot < 0) return false;
                    name = name.Substring(0, dot);
                }
            }

        internal void Check(ISymbol symbol, Location location, ISet<string> findings, bool files, bool http)
        {
            if (symbol == null) return;
            if (symbol is IAliasSymbol alias) { Check(alias.Target, location, findings, files, http); return; }
            if (symbol is IDynamicTypeSymbol) Add(findings, location, "dynamic", "dynamic");
            string ns = symbol is INamespaceSymbol ? symbol.ToDisplayString() : symbol.ContainingNamespace?.ToDisplayString();
            if (DeniedNamespace(symbol is INamespaceSymbol namespaceSymbol ? namespaceSymbol : symbol.ContainingNamespace))
                Add(findings, location, "denied namespace", symbol.ToDisplayString());
            string name = symbol.ToDisplayString();
            string typeName = symbol is INamedTypeSymbol type ? Canonical(type) : name;
            string hostType = symbol is INamedTypeSymbol ? typeName : symbol.ContainingType == null ? null : Canonical(symbol.ContainingType);
            if (IsHostName(ns) && !ApiAllowlist.ToolFacingTypes.Contains(hostType))
                Add(findings, location, "host internal", name);
            if (policy.DeniedTypes.Contains(typeName)) Add(findings, location, "denied type", name);
            // A method group (for example object.GetType) need not have an expression type.
            // Refuse Type-valued symbols as well as references to Type itself.
            var valueType = symbol is IMethodSymbol valueMethod ? valueMethod.ReturnType :
                symbol is IPropertySymbol property ? property.Type :
                symbol is IFieldSymbol field ? field.Type :
                symbol is ILocalSymbol local ? local.Type :
                symbol is IParameterSymbol parameter ? parameter.Type : null;
            if (valueType?.ToDisplayString() == "System.Type")
                Add(findings, location, "denied type", "System.Type");
            if (symbol is IMethodSymbol || symbol is IPropertySymbol || symbol is IEventSymbol || symbol is IFieldSymbol)
            {
                var member = symbol is IMethodSymbol reduced && reduced.ReducedFrom != null ? reduced.ReducedFrom : symbol;
                string memberName = Canonical(member);
                if (policy.DeniedMembers.Contains(memberName)) Add(findings, location, "denied member", memberName);
            }
            if (name == ApiAllowlist.Files && !files) Add(findings, location, "files capability required", name);
            if (name == ApiAllowlist.Http && !http) Add(findings, location, "http capability required", name);
            if (policy.CapabilityGatedMembers.TryGetValue(Canonical(symbol), out var prefix) &&
                !capabilities.Any(c => c.StartsWith(prefix + ":", StringComparison.Ordinal)))
                Add(findings, location, "capability required", Canonical(symbol));
            if (!Exempt(symbol))
            {
                bool allowed = true;
                if (symbol is INamedTypeSymbol namedType)
                    allowed = FullType(namedType) || policy.IdentityTypes.Contains(Canonical(namedType)) ||
                        policy.MemberTypes.Contains(Canonical(namedType));
                else if (symbol is IMethodSymbol || symbol is IPropertySymbol || symbol is IFieldSymbol || symbol is IEventSymbol)
                {
                    var member = symbol is IMethodSymbol reducedMethod && reducedMethod.ReducedFrom != null
                        ? reducedMethod.ReducedFrom : symbol;
                    allowed = FullType(member.ContainingType) || policy.AllowedMembers.Contains(Canonical(member)) ||
                        policy.CapabilityGatedMembers.ContainsKey(Canonical(member));
                }
                if (!allowed) Add(findings, location, "not allowlisted", Canonical(symbol));
            }
            if (symbol.ContainingType != null) Check(symbol.ContainingType, location, findings, files, http);
            if (symbol is IArrayTypeSymbol array) Check(array.ElementType, location, findings, files, http);
            if (symbol is IPointerTypeSymbol pointer) Check(pointer.PointedAtType, location, findings, files, http);
            if (symbol is INamedTypeSymbol named)
                foreach (var argument in named.TypeArguments)
                    if (!(argument is ITypeParameterSymbol)) Check(argument, location, findings, files, http);
            if (symbol is IMethodSymbol method)
                foreach (var argument in method.TypeArguments)
                    if (!(argument is ITypeParameterSymbol)) Check(argument, location, findings, files, http);
        }

        }

        private static bool IsHostName(string name) => name == "LeafSolarDesign.ScriptHost" ||
            (name != null && name.StartsWith("LeafSolarDesign.ScriptHost.", StringComparison.Ordinal));

        private static void Add(ISet<string> findings, Location location, string rule, string symbol)
        {
            var span = location.GetLineSpan();
            findings.Add($"{span.Path}({span.StartLinePosition.Line + 1},{span.StartLinePosition.Character + 1}): scan: {rule}: {symbol}");
        }
    }
}
