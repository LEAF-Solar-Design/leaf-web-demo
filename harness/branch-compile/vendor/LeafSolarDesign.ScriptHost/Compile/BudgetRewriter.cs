using System.Linq;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

namespace LeafSolarDesign.ScriptHost.Compile
{
    // Run only after scanning and binding the ORIGINAL trees. Generated tokens contain
    // no newlines and retain the author's trivia, preserving source line numbers.
    internal sealed class BudgetRewriter : CSharpSyntaxRewriter
    {
        private readonly SemanticModel model;
        internal BudgetRewriter(SemanticModel model) { this.model = model; }

        private static StatementSyntax Guard(bool entry = false) => SyntaxFactory.ParseStatement(
            "global::LeafSolarDesign.ScriptHost.Run.ToolBudget." + (entry ? "Enter" : "Tick") + "();");

        private static BlockSyntax GuardBody(StatementSyntax body, bool entry = false)
        {
            if (body is BlockSyntax block) return block.WithStatements(block.Statements.Insert(0, Guard(entry)));
            return SyntaxFactory.Block(Guard(entry), body);
        }

        private static bool ReturnsValue(IMethodSymbol method) => method != null && !method.ReturnsVoid &&
            !(method.IsAsync && method.ReturnType is INamedTypeSymbol type && type.Arity == 0);

        private static BlockSyntax ExpressionBody(ExpressionSyntax expression, bool returnsValue)
        {
            StatementSyntax statement;
            if (expression is ThrowExpressionSyntax thrown)
                statement = SyntaxFactory.ThrowStatement(thrown.Expression).WithThrowKeyword(thrown.ThrowKeyword);
            else if (returnsValue)
                statement = SyntaxFactory.ReturnStatement(expression).WithReturnKeyword(
                    SyntaxFactory.Token(SyntaxKind.ReturnKeyword).WithTrailingTrivia(SyntaxFactory.Space));
            else statement = SyntaxFactory.ExpressionStatement(expression);
            return SyntaxFactory.Block(Guard(true), statement);
        }

        private static BlockSyntax ArrowBody(ArrowExpressionClauseSyntax arrow, SyntaxToken semicolon, bool returnsValue)
        {
            var block = ExpressionBody(arrow.Expression, returnsValue);
            return block.WithOpenBraceToken(block.OpenBraceToken.WithLeadingTrivia(arrow.ArrowToken.LeadingTrivia)
                    .WithTrailingTrivia(arrow.ArrowToken.TrailingTrivia))
                .WithCloseBraceToken(block.CloseBraceToken.WithLeadingTrivia(semicolon.LeadingTrivia)
                    .WithTrailingTrivia(semicolon.TrailingTrivia));
        }

        public override SyntaxNode VisitCompilationUnit(CompilationUnitSyntax node)
        {
            var rewritten = (CompilationUnitSyntax)base.VisitCompilationUnit(node);
            var members = new System.Collections.Generic.List<MemberDeclarationSyntax>();
            foreach (var member in rewritten.Members)
            {
                if (member is GlobalStatementSyntax global && global.Statement is LabeledStatementSyntax)
                {
                    var statements = new System.Collections.Generic.List<StatementSyntax>();
                    ExpandLabel(global.Statement, statements);
                    members.AddRange(statements.Select(s => SyntaxFactory.GlobalStatement(s)));
                }
                else members.Add(member);
            }
            rewritten = rewritten.WithMembers(SyntaxFactory.List(members));
            var first = rewritten.Members.OfType<GlobalStatementSyntax>().FirstOrDefault();
            if (first == null) return rewritten;
            // Keep #load and other leading directives before the inserted statement.
            var guard = SyntaxFactory.GlobalStatement(Guard()).WithLeadingTrivia(first.GetLeadingTrivia());
            var index = rewritten.Members.IndexOf(first);
            return rewritten.WithMembers(rewritten.Members.Replace(first, first.WithoutLeadingTrivia()).Insert(index, guard));
        }

        public override SyntaxNode VisitForStatement(ForStatementSyntax node)
        { var n = (ForStatementSyntax)base.VisitForStatement(node); return n.WithStatement(GuardBody(n.Statement)); }
        public override SyntaxNode VisitForEachStatement(ForEachStatementSyntax node)
        { var n = (ForEachStatementSyntax)base.VisitForEachStatement(node); return n.WithStatement(GuardBody(n.Statement)); }
        public override SyntaxNode VisitForEachVariableStatement(ForEachVariableStatementSyntax node)
        { var n = (ForEachVariableStatementSyntax)base.VisitForEachVariableStatement(node); return n.WithStatement(GuardBody(n.Statement)); }
        public override SyntaxNode VisitWhileStatement(WhileStatementSyntax node)
        { var n = (WhileStatementSyntax)base.VisitWhileStatement(node); return n.WithStatement(GuardBody(n.Statement)); }
        public override SyntaxNode VisitDoStatement(DoStatementSyntax node)
        { var n = (DoStatementSyntax)base.VisitDoStatement(node); return n.WithStatement(GuardBody(n.Statement)); }
        public override SyntaxNode VisitCatchClause(CatchClauseSyntax node)
        { var n = (CatchClauseSyntax)base.VisitCatchClause(node); return n.WithBlock(GuardBody(n.Block)); }

        public override SyntaxNode VisitBlock(BlockSyntax node)
        {
            var n = (BlockSyntax)base.VisitBlock(node);
            // A label must stay in its original scope (locals may be used after it).
            var statements = new System.Collections.Generic.List<StatementSyntax>();
            foreach (var statement in n.Statements) ExpandLabel(statement, statements);
            return n.WithStatements(SyntaxFactory.List(statements));
        }

        private static void ExpandLabel(StatementSyntax statement, System.Collections.Generic.List<StatementSyntax> result)
        {
            if (statement is LabeledStatementSyntax label)
            {
                result.Add(label.WithStatement(Guard()));
                ExpandLabel(label.Statement, result);
            }
            else result.Add(statement);
        }

        public override SyntaxNode VisitLabeledStatement(LabeledStatementSyntax node)
        {
            var n = (LabeledStatementSyntax)base.VisitLabeledStatement(node);
            // Block-owned labels are expanded without introducing a scope above.
            if (node.Parent is BlockSyntax || node.Parent is SwitchSectionSyntax ||
                node.Parent is GlobalStatementSyntax || node.Parent is LabeledStatementSyntax) return n;
            var statements = new System.Collections.Generic.List<StatementSyntax> { Guard() };
            ExpandLabel(n.Statement, statements);
            return n.WithStatement(SyntaxFactory.Block(statements));
        }

        public override SyntaxNode VisitSwitchSection(SwitchSectionSyntax node)
        {
            var n = (SwitchSectionSyntax)base.VisitSwitchSection(node);
            var statements = new System.Collections.Generic.List<StatementSyntax> { Guard() };
            foreach (var statement in n.Statements) ExpandLabel(statement, statements);
            return n.WithStatements(SyntaxFactory.List(statements));
        }

        public override SyntaxNode VisitMethodDeclaration(MethodDeclarationSyntax node)
        {
            var n = (MethodDeclarationSyntax)base.VisitMethodDeclaration(node);
            if (n.Body != null) return n.WithBody(GuardBody(n.Body, true));
            return n.ExpressionBody == null ? n : n.WithBody(ArrowBody(n.ExpressionBody, n.SemicolonToken,
                ReturnsValue(model.GetDeclaredSymbol(node)))).WithExpressionBody(null).WithSemicolonToken(default);
        }

        public override SyntaxNode VisitLocalFunctionStatement(LocalFunctionStatementSyntax node)
        {
            var n = (LocalFunctionStatementSyntax)base.VisitLocalFunctionStatement(node);
            if (n.Body != null) return n.WithBody(GuardBody(n.Body, true));
            return n.ExpressionBody == null ? n : n.WithBody(ArrowBody(n.ExpressionBody, n.SemicolonToken,
                ReturnsValue(model.GetDeclaredSymbol(node)))).WithExpressionBody(null).WithSemicolonToken(default);
        }

        public override SyntaxNode VisitConstructorDeclaration(ConstructorDeclarationSyntax node)
        {
            var n = (ConstructorDeclarationSyntax)base.VisitConstructorDeclaration(node);
            if (n.Body != null) return n.WithBody(GuardBody(n.Body, true));
            return n.ExpressionBody == null ? n : n.WithBody(ArrowBody(n.ExpressionBody, n.SemicolonToken, false))
                .WithExpressionBody(null).WithSemicolonToken(default);
        }

        public override SyntaxNode VisitDestructorDeclaration(DestructorDeclarationSyntax node)
        {
            var n = (DestructorDeclarationSyntax)base.VisitDestructorDeclaration(node);
            if (n.Body != null) return n.WithBody(GuardBody(n.Body, true));
            return n.ExpressionBody == null ? n : n.WithBody(ArrowBody(n.ExpressionBody, n.SemicolonToken, false))
                .WithExpressionBody(null).WithSemicolonToken(default);
        }

        public override SyntaxNode VisitAccessorDeclaration(AccessorDeclarationSyntax node)
        {
            var n = (AccessorDeclarationSyntax)base.VisitAccessorDeclaration(node);
            if (n.Body != null) return n.WithBody(GuardBody(n.Body, true));
            return n.ExpressionBody == null ? n : n.WithBody(ArrowBody(n.ExpressionBody, n.SemicolonToken,
                node.IsKind(SyntaxKind.GetAccessorDeclaration))).WithExpressionBody(null).WithSemicolonToken(default);
        }

        private static AccessorListSyntax Getter(ArrowExpressionClauseSyntax arrow, SyntaxToken semicolon) =>
            SyntaxFactory.AccessorList(SyntaxFactory.SingletonList(SyntaxFactory.AccessorDeclaration(SyntaxKind.GetAccessorDeclaration)
                .WithBody(ArrowBody(arrow, semicolon, true))));

        public override SyntaxNode VisitPropertyDeclaration(PropertyDeclarationSyntax node)
        {
            var n = (PropertyDeclarationSyntax)base.VisitPropertyDeclaration(node);
            return n.ExpressionBody == null ? n : n.WithAccessorList(Getter(n.ExpressionBody, n.SemicolonToken))
                .WithExpressionBody(null).WithSemicolonToken(default);
        }

        public override SyntaxNode VisitIndexerDeclaration(IndexerDeclarationSyntax node)
        {
            var n = (IndexerDeclarationSyntax)base.VisitIndexerDeclaration(node);
            return n.ExpressionBody == null ? n : n.WithAccessorList(Getter(n.ExpressionBody, n.SemicolonToken))
                .WithExpressionBody(null).WithSemicolonToken(default);
        }

        public override SyntaxNode VisitOperatorDeclaration(OperatorDeclarationSyntax node)
        {
            var n = (OperatorDeclarationSyntax)base.VisitOperatorDeclaration(node);
            if (n.Body != null) return n.WithBody(GuardBody(n.Body, true));
            return n.ExpressionBody == null ? n : n.WithBody(ArrowBody(n.ExpressionBody, n.SemicolonToken, true))
                .WithExpressionBody(null).WithSemicolonToken(default);
        }

        public override SyntaxNode VisitConversionOperatorDeclaration(ConversionOperatorDeclarationSyntax node)
        {
            var n = (ConversionOperatorDeclarationSyntax)base.VisitConversionOperatorDeclaration(node);
            if (n.Body != null) return n.WithBody(GuardBody(n.Body, true));
            return n.ExpressionBody == null ? n : n.WithBody(ArrowBody(n.ExpressionBody, n.SemicolonToken, true))
                .WithExpressionBody(null).WithSemicolonToken(default);
        }

        private CSharpSyntaxNode LambdaBody(AnonymousFunctionExpressionSyntax original, CSharpSyntaxNode body) =>
            body is BlockSyntax block ? GuardBody(block, true) : ExpressionBody((ExpressionSyntax)body,
                ReturnsValue(model.GetSymbolInfo(original).Symbol as IMethodSymbol));

        public override SyntaxNode VisitSimpleLambdaExpression(SimpleLambdaExpressionSyntax node)
        { var n = (SimpleLambdaExpressionSyntax)base.VisitSimpleLambdaExpression(node); return n.WithBody(LambdaBody(node, n.Body)); }
        public override SyntaxNode VisitParenthesizedLambdaExpression(ParenthesizedLambdaExpressionSyntax node)
        { var n = (ParenthesizedLambdaExpressionSyntax)base.VisitParenthesizedLambdaExpression(node); return n.WithBody(LambdaBody(node, n.Body)); }
        public override SyntaxNode VisitAnonymousMethodExpression(AnonymousMethodExpressionSyntax node)
        { var n = (AnonymousMethodExpressionSyntax)base.VisitAnonymousMethodExpression(node); return n.WithBlock(GuardBody(n.Block, true)); }
    }
}
