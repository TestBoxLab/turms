from typing import Dict, Set
from graphql.utilities.build_client_schema import GraphQLSchema
from graphql.language.ast import DocumentNode, FieldNode
from graphql import (
    GraphQLEnumType,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLInputObjectType,
    GraphQLScalarType,
    ListTypeNode,
    NamedTypeNode,
    NonNullTypeNode,
    OperationDefinitionNode,
    FragmentDefinitionNode,
    GraphQLInterfaceType,
)
from graphql.type.definition import (
    GraphQLType,
    GraphQLUnionType,
)
from graphql.language.ast import (
    FragmentSpreadNode,
    InlineFragmentNode,
)
from turms.utils import parse_documents


class ReferenceRegistry:
    def __init__(self):
        self.objects: Set[str] = set()
        self.fragments: Set[str] = set()
        self.enums: Set[str] = set()
        self.inputs: Set[str] = set()
        self.scalars: Set[str] = set()
        self.operations: Set[str] = set()

    def register_type(self, type_name: str):
        self.objects.add(type_name)

    def register_fragment(self, type_name: str):
        self.fragments.add(type_name)

    def register_enum(self, type_name: str):
        self.enums.add(type_name)

    def register_input(self, type_name: str):
        self.inputs.add(type_name)

    def register_scalar(self, type_name: str):
        self.scalars.add(type_name)


def recurse_find_references(
    node: FieldNode,
    graphql_type: GraphQLType,
    client_schema: GraphQLSchema,
    registry: ReferenceRegistry,
    is_optional=True,
):
    if isinstance(graphql_type, GraphQLUnionType):

        for sub_node in node.selection_set.selections:

            if isinstance(sub_node, FragmentSpreadNode):
                registry.register_fragment(sub_node.name.value)

            if isinstance(sub_node, InlineFragmentNode):
                for sub_sub_node in sub_node.selection_set.selections:

                    if isinstance(sub_sub_node, FieldNode):
                        sub_sub_node_type = client_schema.get_type(
                            sub_node.type_condition.name.value
                        )

                        if sub_sub_node.name.value == "__typename":
                            continue

                        field_type = sub_sub_node_type.fields[sub_sub_node.name.value]
                        return recurse_find_references(
                            sub_sub_node,
                            field_type.type,
                            client_schema,
                            registry,
                        )

    elif isinstance(graphql_type, GraphQLInterfaceType):
        # Lets Create Base Class to Inherit from for this

        for sub_node in node.selection_set.selections:

            if isinstance(sub_node, FieldNode):
                if sub_node.name.value == "__typename":
                    continue

                field_type = graphql_type.fields[sub_node.name.value]
                recurse_find_references(
                    sub_node,
                    field_type.type,
                    client_schema,
                    registry,
                )

            if isinstance(sub_node, FragmentSpreadNode):
                registry.register_fragment(sub_node.name.value)

            if isinstance(sub_node, InlineFragmentNode):

                for sub_sub_node in sub_node.selection_set.selections:

                    if isinstance(sub_sub_node, FieldNode):
                        sub_sub_node_type = client_schema.get_type(
                            sub_node.type_condition.name.value
                        )

                        if sub_sub_node.name.value == "__typename":
                            continue

                        field_type = sub_sub_node_type.fields[sub_sub_node.name.value]
                        recurse_find_references(
                            sub_sub_node,
                            field_type.type,
                            client_schema,
                            registry,
                        )

    elif isinstance(graphql_type, GraphQLObjectType):

        for sub_node in node.selection_set.selections:

            if isinstance(sub_node, FieldNode):
                if sub_node.name.value == "__typename":
                    continue

                field_type = graphql_type.fields[sub_node.name.value]
                recurse_find_references(
                    sub_node,
                    field_type.type,
                    client_schema,
                    registry,
                )

            if isinstance(sub_node, FragmentSpreadNode):
                registry.register_fragment(sub_node.name.value)

            if isinstance(sub_node, InlineFragmentNode):

                for sub_sub_node in sub_node.selection_set.selections:

                    if isinstance(sub_sub_node, FieldNode):
                        sub_sub_node_type = client_schema.get_type(
                            sub_node.type_condition.name.value
                        )

                        if sub_sub_node.name.value == "__typename":
                            continue

                        field_type = sub_sub_node_type.fields[sub_sub_node.name.value]
                        recurse_find_references(
                            sub_sub_node,
                            field_type.type,
                            client_schema,
                            registry,
                        )

    elif isinstance(graphql_type, GraphQLScalarType):

        registry.register_scalar(graphql_type.name)

    elif isinstance(graphql_type, GraphQLEnumType):

        registry.register_enum(graphql_type.name)

    elif isinstance(graphql_type, GraphQLNonNull):
        recurse_find_references(
            node,
            graphql_type.of_type,
            client_schema,
            registry,
            is_optional=False,
        )

    elif isinstance(graphql_type, GraphQLList):

        recurse_find_references(
            node,
            graphql_type.of_type,
            client_schema,
            registry,
            is_optional=False,
        )
    else:
        raise Exception("Unknown Type", type(graphql_type), graphql_type)


def recurse_type_annotation(
    graphql_type: NamedTypeNode,
    schema: GraphQLSchema,
    registry: ReferenceRegistry,
    optional=True,
):

    if isinstance(graphql_type, NonNullTypeNode):
        return recurse_type_annotation(
            graphql_type.type, schema, registry, optional=False
        )

    elif isinstance(graphql_type, ListTypeNode):
        recurse_type_annotation(graphql_type.type, schema, registry)

    elif isinstance(graphql_type, NamedTypeNode):

        z = schema.get_type(graphql_type.name.value)
        if isinstance(z, GraphQLScalarType):
            registry.register_scalar(z.name)

        elif isinstance(z, GraphQLInputObjectType):
            registry.register_input(z.name)

        elif isinstance(z, GraphQLEnumType):
            registry.register_enum(z.name)

        else:
            raise Exception("Unknown Subtype", type(graphql_type), graphql_type)

    else:
        raise Exception("Unknown Type", type(graphql_type), graphql_type)


def create_reference_registry_from_documents(
    schema: GraphQLSchema, document: DocumentNode
) -> ReferenceRegistry:
    """Finds all references of types of types that are used in the documents"""

    fragments: Dict[str, FragmentDefinitionNode] = {}
    operations: Dict[str, OperationDefinitionNode] = {}

    registry = ReferenceRegistry()

    for definition in document.definitions:
        if isinstance(definition, FragmentDefinitionNode):
            fragments[definition.name.value] = definition
        if isinstance(definition, OperationDefinitionNode):
            operations[definition.name.value] = definition

    for fragment in fragments.values():

        type = schema.get_type(fragment.type_condition.name.value)
        for selection in fragment.selection_set.selections:

            if isinstance(selection, FieldNode):
                # definition
                if selection.name.value == "__typename":
                    continue
                this_type = type.fields[selection.name.value]

                recurse_find_references(
                    selection,
                    this_type.type,
                    schema,
                    registry,
                )

    for operation in operations.values():

        type = schema.get_root_type(operation.operation)

        for argument in operation.variable_definitions:
            recurse_type_annotation(argument.type, schema, registry)

        for selection in operation.selection_set.selections:

            if isinstance(selection, FieldNode):
                # definition
                if selection.name.value == "__typename":
                    continue
                this_type = type.fields[selection.name.value]

                recurse_find_references(
                    selection,
                    this_type.type,
                    schema,
                    registry,
                )

    return registry
