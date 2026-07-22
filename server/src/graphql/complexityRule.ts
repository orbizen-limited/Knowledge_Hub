import { GraphQLError, type ASTVisitor, type ValidationContext } from 'graphql';

// Lightweight standalone replacement for `graphql-query-complexity`: that
// package's ESM build imports 'graphql/index.mjs' directly, a separate module
// instance from the 'graphql' entry everything else in this process uses,
// which graphql-js treats as cross-realm and rejects at validation time.
// Counting selected fields is a coarser cost signal than field-weighted
// complexity, but combined with depthLimit it closes the same nested-query
// resource-exhaustion gap (OWASP API4) without the dependency conflict.
export function fieldCountLimit(maxFields: number) {
  return function fieldCountLimitRule(context: ValidationContext): ASTVisitor {
    let count = 0;
    return {
      Field() {
        count += 1;
        if (count > maxFields) {
          context.reportError(
            new GraphQLError(
              `Query selects too many fields (limit is ${maxFields}).`,
              { nodes: undefined },
            ),
          );
        }
      },
    };
  };
}
