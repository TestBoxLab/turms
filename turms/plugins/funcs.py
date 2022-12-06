from __future__ import annotations

import ast
import logging
import re
from ctypes import Union
from enum import Enum
from typing import Any, List, Optional, Tuple
from graphql.language.print_location import print_location


from graphql import (
    BooleanValueNode,
    FieldDefinitionNode,
    FloatValueNode,
    FragmentSpreadNode,
    GraphQLField,
    GraphQLInputType,
    GraphQLNamedType,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLScalarType,
    IntValueNode,
    NamedTypeNode,
    NullValueNode,
    StringValueNode,
    ValueNode,
    VariableDefinitionNode,
    is_wrapping_type,
)
from graphql.language.ast import (
    FieldNode,
    ListTypeNode,
    NonNullTypeNode,
    OperationDefinitionNode,
    OperationType,
)
from graphql.type.definition import GraphQLList
from graphql.utilities.build_client_schema import GraphQLSchema
from graphql.utilities.get_operation_root_type import get_operation_root_type
from graphql.utilities.type_info import get_field_def
from pydantic import BaseModel, Field
from turms.config import GeneratorConfig
from turms.plugins.base import Plugin, PluginConfig
from turms.registry import ClassRegistry
from turms.utils import (
    NoDocumentsFoundError,
    inspect_operation_for_documentation,
    parse_documents,
    parse_value_node,
    recurse_outputtype_annotation,
    recurse_outputtype_label,
    recurse_type_annotation,
    recurse_type_label,
    target_from_node,
)

logger = logging.getLogger(__name__)


class Kwarg(BaseModel):
    key: str
    type: str
    description: str = "Specify that in turms.plugin.funcs.OperationsFuncPlugin"
    default: Any = None


class Arg(BaseModel):
    key: str
    type: str
    description: str = "Specify that in turms.plugin.funcs.OperationsFuncPlugin"


class FunctionDefinition(BaseModel):
    type: OperationType
    is_async: bool = False
    extra_args: List[Arg] = []
    extra_kwargs: List[Kwarg] = []
    use: str


class FuncsPluginConfig(PluginConfig):
    type = "turms.plugins.funcs.FuncsPlugin"
    funcs_glob: Optional[str]
    prepend_sync: str = ""
    prepend_async: str = "a"
    collapse_lonely: bool = True
    generate_protocol: bool = False
    global_args: List[Arg] = []
    global_kwargs: List[Kwarg] = []
    definitions: List[FunctionDefinition] = []
    extract_documentation: bool = True

    class Config:
        env_prefix = "TURMS_PLUGINS_FUNCS_"


def camel_to_snake(name):
    name = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", name).lower()


def generate_async_func_name(
    o: OperationDefinitionNode,
    plugin_config: FuncsPluginConfig,
    config: GeneratorConfig,
    registry: ClassRegistry,
):

    return f"{plugin_config.prepend_async}{camel_to_snake(o.name.value)}"


def generate_sync_func_name(
    o: OperationDefinitionNode,
    plugin_config: FuncsPluginConfig,
    config: GeneratorConfig,
    registry: ClassRegistry,
):

    return f"{plugin_config.prepend_sync}{camel_to_snake(o.name.value)}"


def get_extra_args_for_onode(
    definition: FunctionDefinition,
    plugin_config: FuncsPluginConfig,
) -> List[Arg]:

    args = plugin_config.global_args
    return args + definition.extra_args


def generate_passing_extra_args_for_onode(
    definition: FunctionDefinition, plugin_config: FuncsPluginConfig
):

    return [
        ast.Name(id=arg.key, ctx=ast.Load())
        for arg in get_extra_args_for_onode(definition, plugin_config)
    ]


def generate_passing_extra_kwargs_for_onode(
    definition: FunctionDefinition, plugin_config: FuncsPluginConfig
):

    return [
        ast.keyword(arg=kwarg.key, value=ast.Name(id=kwarg.key, ctx=ast.Load()))
        for kwarg in get_extra_kwargs_for_onode(definition, plugin_config)
    ]


def get_extra_kwargs_for_onode(
    definition: FunctionDefinition,
    plugin_config: FuncsPluginConfig,
) -> List[Kwarg]:

    kwargs = plugin_config.global_kwargs

    return kwargs + definition.extra_kwargs


def get_definitions_for_onode(
    operation_definition: OperationDefinitionNode,
    plugin_config: FuncsPluginConfig,
) -> List[Arg]:
    """Checks the Plugin Config if the operation definition should be included
    in the generated functions

    Args:
        operation_definition (OperationDefinitionNode): _description_
        plugin_config (FuncsPluginConfig): _description_

    Returns:
        List[Arg]: _description_
    """

    definitions = [
        definition
        for definition in plugin_config.definitions
        if definition.type == operation_definition.operation
    ]

    return definitions


def generate_parameters(
    definition: FunctionDefinition,
    operation_definition: OperationDefinitionNode,
    config: GeneratorConfig,
    plugin_config: FuncsPluginConfig,
    registry: ClassRegistry,
):

    extra_args = get_extra_args_for_onode(definition, plugin_config)
    pos_args = []

    for arg in extra_args:
        registry.register_import(arg.type)
        pos_args.append(
            ast.arg(
                arg=arg.key,
                annotation=ast.Name(
                    id=arg.type.split(".")[-1],
                    ctx=ast.Load(),
                ),
            )
        )

    arg_variables = [
        v
        for v in operation_definition.variable_definitions
        if isinstance(v.type, NonNullTypeNode) and not v.default_value
    ]
    kwarg_variables = [
        v
        for v in operation_definition.variable_definitions
        if not isinstance(v.type, NonNullTypeNode) or v.default_value
    ]

    for v in arg_variables:
        pos_args.append(
            ast.arg(
                arg=registry.generate_parameter_name(v.variable.name.value),
                annotation=recurse_type_annotation(v.type, registry),
            )
        )

    kw_args = []
    kw_values = []

    for v in kwarg_variables:
        kw_args.append(
            ast.arg(
                arg=registry.generate_parameter_name(v.variable.name.value),
                annotation=recurse_type_annotation(v.type, registry),
            )
        )
        kw_values.append(
            ast.Constant(
                value=parse_value_node(v.default_value) if v.default_value else None
            )
        )

    extra_kwargs = get_extra_kwargs_for_onode(definition, plugin_config)

    for kwarg in extra_kwargs:
        registry.register_import(kwarg.type)
        kw_args.append(
            ast.arg(
                arg=kwarg.key,
                annotation=ast.Name(
                    id=kwarg.type.split(".")[-1],
                    ctx=ast.Load(),
                ),
            )
        )
        kw_values.append(ast.Constant(value=kwarg.default))

    return ast.arguments(
        args=pos_args + kw_args,
        posonlyargs=[],
        kwonlyargs=[],
        kw_defaults=[],
        defaults=kw_values,
    )


def generate_variable_dict(o: OperationDefinitionNode, registry: ClassRegistry):

    keys = []
    values = []

    for v in o.variable_definitions:
        keys.append(ast.Constant(value=v.variable.name.value))
        values.append(
            ast.Name(
                id=registry.generate_parameter_name(v.variable.name.value),
                ctx=ast.Load(),
            )
        )

    return ast.Dict(keys=keys, values=values)


def generate_document_arg(o: OperationDefinitionNode, registry: ClassRegistry):

    return ast.Name(id=get_operation_class_name(o, registry), ctx=ast.Load())


def get_operation_class_name(
    o: OperationDefinitionNode, registry: ClassRegistry
) -> str:
    """Generates the name of the Operation Class for the given OperationDefinitionNode



    Args:
        o (OperationDefinitionNode): The graphql o node
        registry (ClassRegistry): The registry (used to get the operation class name)

    Raises:
        Exception: If the operation type is not supported

    Returns:
        str: _description_
    """

    if o.operation == OperationType.QUERY:
        return registry.style_query_class(o.name.value)
    if o.operation == OperationType.MUTATION:
        return registry.style_mutation_class(o.name.value)
    if o.operation == OperationType.SUBSCRIPTION:
        return registry.style_subscription_class(o.name.value)

    raise Exception("Incorrect Operation Type ")  # pragma: no cover


def get_return_type_annotation(
    o: OperationDefinitionNode,
    client_schema: GraphQLSchema,
    registry: ClassRegistry,
    collapse: bool = True,
) -> ast.AST:
    """Gets the return type annotation for the given operation definition node

    Ulized an autocollapse feature to collapse the return type annotation if it is a single fragment,
    to not generate unnecessary classes

    """

    o_name = get_operation_class_name(o, registry)
    root = get_operation_root_type(client_schema, o)

    if collapse is True:
        collapsable_field = o.selection_set.selections[0]
        field_definition = get_field_def(client_schema, root, collapsable_field)

        if collapsable_field.selection_set is None:  # pragma: no cover
            return recurse_outputtype_annotation(field_definition.type, registry)

        if (
            len(collapsable_field.selection_set.selections) == 1
        ):  # Dealing with one Element
            collapsable_fragment_field = collapsable_field.selection_set.selections[0]
            if isinstance(
                collapsable_fragment_field, FragmentSpreadNode
            ):  # Dealing with a on element fragment
                return recurse_outputtype_annotation(
                    field_definition.type,
                    registry,
                    overwrite_final=registry.reference_fragment(
                        collapsable_fragment_field.name.value, "", allow_forward=False
                    ).id,
                )

        field_name = (
            collapsable_field.name.value
            if not collapsable_field.alias
            else collapsable_field.alias.value
        )

        return recurse_outputtype_annotation(
            field_definition.type,
            registry,
            overwrite_final=f"{o_name}{field_name.capitalize()}",
        )

    return ast.Name(
        id=o_name,
        ctx=ast.Load(),
    )


def get_return_type_string(
    o: OperationDefinitionNode,
    client_schema: GraphQLSchema,
    registry: ClassRegistry,
    collapse=True,
) -> Tuple[str, bool]:

    o_name = get_operation_class_name(o, registry)

    root = get_operation_root_type(client_schema, o)

    if collapse is True:
        potential_return_field = o.selection_set.selections[0]
        potential_return_type = get_field_def(
            client_schema, root, potential_return_field
        )

        if (
            potential_return_field.selection_set is None
        ):  # Dealing with a scalar type  # pragma: no cover
            return recurse_outputtype_label(potential_return_type.type, registry)

        if (
            len(potential_return_field.selection_set.selections) == 1
        ):  # Dealing with one Element
            collapsable_field = potential_return_field.selection_set.selections[0]

            if isinstance(
                collapsable_field, FragmentSpreadNode
            ):  # Dealing with a on element fragment
                return recurse_outputtype_label(
                    potential_return_type.type,
                    registry,
                    overwrite_final=registry.reference_fragment(
                        collapsable_field.name.value, "", allow_forward=False
                    ).id,
                )

        return recurse_outputtype_label(
            potential_return_type.type,
            registry,
            overwrite_final=f"{o_name}{potential_return_field.name.value.capitalize()}",
        )

    else:
        return o_name


def generate_query_doc(
    definition: FunctionDefinition,
    o: OperationDefinitionNode,
    client_schema: GraphQLSchema,
    config: GeneratorConfig,
    plugin_config: FuncsPluginConfig,
    registry: ClassRegistry,
    collapse=False,
):

    x = get_operation_root_type(client_schema, o)
    o.__annotations__

    o_name = get_operation_class_name(o, registry)

    return_type = get_return_type_string(o, client_schema, registry, collapse)

    header = f"{o.name.value} \n\n"

    operation_documentation = (
        inspect_operation_for_documentation(o)
        if plugin_config.extract_documentation
        else None
    )

    op_descriptions = []

    if not operation_documentation:
        op_descriptions = []

        for field in o.selection_set.selections:
            if isinstance(field, FieldNode):
                target = target_from_node(field)
                operation_type = get_field_def(client_schema, x, field).type
                while is_wrapping_type(operation_type):
                    operation_type = operation_type.of_type
                if operation_type.description:
                    op_descriptions.append(f"{target}: {operation_type.description}\n")

        description = "\n ".join([header] + op_descriptions)

    else:
        description = header + operation_documentation

    description += "\n\nArguments:\n"

    extra_args = get_extra_args_for_onode(definition, plugin_config)

    for arg in extra_args:
        description += f"    {arg.key} ({arg.type}): {arg.description}\n"

    for v in o.variable_definitions:
        if isinstance(v.type, NonNullTypeNode) and not v.default_value:
            description += f"    {registry.generate_parameter_name(v.variable.name.value)} ({recurse_type_label(v.type, registry)}): {v.variable.name.value}\n"

    for v in o.variable_definitions:
        if not isinstance(v.type, NonNullTypeNode) or v.default_value:
            description += f"    {registry.generate_parameter_name(v.variable.name.value)} ({recurse_type_label(v.type, registry)}, optional): {v.variable.name.value}. {'' if not v.default_value else  'Defaults to ' + str(v.default_value.value)}\n"

    extra_kwargs = get_extra_kwargs_for_onode(definition, plugin_config)
    for kwarg in extra_kwargs:
        description += (
            f"    {kwarg.key} ({kwarg.type}, optional): {kwarg.description}\n"
        )

    description += "\nReturns:\n"
    description += f"    {return_type}"

    return ast.Expr(value=ast.Constant(value=description))


def genereate_async_call(
    definition: FunctionDefinition,
    o: OperationDefinitionNode,
    client_schema: GraphQLSchema,
    config: GeneratorConfig,
    plugin_config: FuncsPluginConfig,
    registry: ClassRegistry,
    collapse=False,
):
    registry.register_import(definition.use)

    if not collapse:
        return ast.Return(
            value=ast.Await(
                value=ast.Call(
                    func=ast.Name(
                        id=definition.use.split(".")[-1],
                        ctx=ast.Load(),
                    ),
                    keywords=generate_passing_extra_kwargs_for_onode(
                        definition, plugin_config
                    ),
                    args=generate_passing_extra_args_for_onode(
                        definition, plugin_config
                    )
                    + [
                        generate_document_arg(o, registry),
                        generate_variable_dict(o, registry),
                    ],
                )
            )
        )
    else:

        return ast.Return(
            value=ast.Attribute(
                value=ast.Await(
                    value=ast.Call(
                        func=ast.Name(
                            id=definition.use.split(".")[-1],
                            ctx=ast.Load(),
                        ),
                        keywords=generate_passing_extra_kwargs_for_onode(
                            definition, plugin_config
                        ),
                        args=generate_passing_extra_args_for_onode(
                            definition, plugin_config
                        )
                        + [
                            generate_document_arg(o, registry),
                            generate_variable_dict(o, registry),
                        ],
                    )
                ),
                attr=registry.generate_node_name(
                    o.selection_set.selections[0].name.value
                ),
                ctx=ast.Load(),
            )
        )


def genereate_sync_call(
    definition: FunctionDefinition,
    o: OperationDefinitionNode,
    client_schema: GraphQLSchema,
    config: GeneratorConfig,
    plugin_config: FuncsPluginConfig,
    registry: ClassRegistry,
    collapse=False,
):
    registry.register_import(definition.use)
    if not collapse:
        return ast.Return(
            value=ast.Call(
                func=ast.Name(
                    id=definition.use.split(".")[-1],
                    ctx=ast.Load(),
                ),
                keywords=generate_passing_extra_kwargs_for_onode(
                    definition, plugin_config
                ),
                args=generate_passing_extra_args_for_onode(definition, plugin_config)
                + [
                    generate_document_arg(o, registry),
                    generate_variable_dict(o, registry),
                ],
            )
        )
    else:
        return ast.Return(
            value=ast.Attribute(
                value=ast.Call(
                    func=ast.Name(
                        id=definition.use.split(".")[-1],
                        ctx=ast.Load(),
                    ),
                    keywords=generate_passing_extra_kwargs_for_onode(
                        definition, plugin_config
                    ),
                    args=generate_passing_extra_args_for_onode(
                        definition, plugin_config
                    )
                    + [
                        generate_document_arg(o, registry),
                        generate_variable_dict(o, registry),
                    ],
                ),
                attr=registry.generate_node_name(
                    o.selection_set.selections[0].name.value
                ),
                ctx=ast.Load(),
            )
        )


def genereate_async_iterator(
    definition: FunctionDefinition,
    o: OperationDefinitionNode,
    client_schema: GraphQLSchema,
    config: GeneratorConfig,
    plugin_config: FuncsPluginConfig,
    registry: ClassRegistry,
    collapse=False,
):
    registry.register_import(definition.use)
    if not collapse:
        return ast.AsyncFor(
            target=ast.Name(id="event", ctx=ast.Store()),
            iter=ast.Call(
                func=ast.Name(
                    id=definition.use.split(".")[-1],
                    ctx=ast.Load(),
                ),
                keywords=generate_passing_extra_kwargs_for_onode(
                    definition, plugin_config
                ),
                args=generate_passing_extra_args_for_onode(definition, plugin_config)
                + [
                    generate_document_arg(o, registry),
                    generate_variable_dict(o, registry),
                ],
            ),
            body=[
                ast.Expr(value=ast.Yield(value=ast.Name(id="event", ctx=ast.Load()))),
            ],
            orelse=[],
        )
    else:
        return ast.AsyncFor(
            target=ast.Name(id="event", ctx=ast.Store()),
            iter=ast.Call(
                func=ast.Name(
                    id=definition.use.split(".")[-1],
                    ctx=ast.Load(),
                ),
                keywords=generate_passing_extra_kwargs_for_onode(
                    definition, plugin_config
                ),
                args=generate_passing_extra_args_for_onode(definition, plugin_config)
                + [
                    generate_document_arg(o, registry),
                    generate_variable_dict(o, registry),
                ],
            ),
            body=[
                ast.Expr(
                    value=ast.Yield(
                        value=ast.Attribute(
                            value=ast.Name(id="event", ctx=ast.Load()),
                            ctx=ast.Load(),
                            attr=registry.generate_node_name(
                                o.selection_set.selections[0].name.value
                            ),
                        )
                    )
                ),
            ],
            orelse=[],
        )


def genereate_sync_iterator(
    definition: FunctionDefinition,
    o: OperationDefinitionNode,
    client_schema: GraphQLSchema,
    config: GeneratorConfig,
    plugin_config: FuncsPluginConfig,
    registry: ClassRegistry,
    collapse=False,
):
    registry.register_import(definition.use)
    if not collapse:
        return ast.For(
            target=ast.Name(id="event", ctx=ast.Store()),
            iter=ast.Call(
                func=ast.Name(
                    id=definition.use.split(".")[-1],
                    ctx=ast.Load(),
                ),
                keywords=generate_passing_extra_kwargs_for_onode(
                    definition, plugin_config
                ),
                args=generate_passing_extra_args_for_onode(definition, plugin_config)
                + [
                    generate_document_arg(o, registry),
                    generate_variable_dict(o, registry),
                ],
            ),
            body=[
                ast.Expr(value=ast.Yield(value=ast.Name(id="event", ctx=ast.Load()))),
            ],
            orelse=[],
        )
    else:
        return ast.For(
            target=ast.Name(id="event", ctx=ast.Store()),
            iter=ast.Call(
                func=ast.Name(
                    id=definition.use.split(".")[-1],
                    ctx=ast.Load(),
                ),
                keywords=generate_passing_extra_kwargs_for_onode(
                    definition, plugin_config
                ),
                args=generate_passing_extra_args_for_onode(definition, plugin_config)
                + [
                    generate_document_arg(o, registry),
                    generate_variable_dict(o, registry),
                ],
            ),
            body=[
                ast.Expr(
                    value=ast.Yield(
                        value=ast.Attribute(
                            value=ast.Name(id="event", ctx=ast.Load()),
                            ctx=ast.Load(),
                            attr=registry.generate_node_name(
                                o.selection_set.selections[0].name.value
                            ),
                        )
                    )
                ),
            ],
            orelse=[],
        )


def is_collapsable(o: OperationDefinitionNode):
    assert o.selection_set is not None, "Operation needs to have at least a selection"
    return len(o.selection_set.selections) == 1


def generate_operation_func(
    definition: FunctionDefinition,
    o: OperationDefinitionNode,
    client_schema: GraphQLSchema,
    config: GeneratorConfig,
    plugin_config: FuncsPluginConfig,
    registry: ClassRegistry,
):
    tree = []

    collapse = plugin_config.collapse_lonely and is_collapsable(o)

    return_type = get_return_type_annotation(
        o, client_schema, registry, collapse=collapse
    )

    doc = generate_query_doc(
        definition, o, client_schema, config, plugin_config, registry, collapse
    )

    if definition.is_async:
        if o.operation == OperationType.SUBSCRIPTION:
            registry.register_import("typing.AsyncIterator")

        tree.append(
            ast.AsyncFunctionDef(
                name=generate_async_func_name(o, plugin_config, config, registry),
                args=generate_parameters(
                    definition,
                    o,
                    config,
                    plugin_config,
                    registry,
                ),
                body=[
                    doc,
                    genereate_async_call(
                        definition,
                        o,
                        client_schema,
                        config,
                        plugin_config,
                        registry,
                        collapse,
                    )
                    if definition.type != OperationType.SUBSCRIPTION
                    else genereate_async_iterator(
                        definition,
                        o,
                        client_schema,
                        config,
                        plugin_config,
                        registry,
                        collapse,
                    ),
                ],
                decorator_list=[],
                returns=return_type
                if definition.type != OperationType.SUBSCRIPTION
                else ast.Subscript(
                    value=ast.Name(id="AsyncIterator", ctx=ast.Load()),
                    slice=return_type,
                ),
            )
        )

    if not definition.is_async:
        if o.operation == OperationType.SUBSCRIPTION:
            registry.register_import("typing.Iterator")

        tree.append(
            ast.FunctionDef(
                name=generate_sync_func_name(o, plugin_config, config, registry),
                args=generate_parameters(
                    definition,
                    o,
                    config,
                    plugin_config,
                    registry,
                ),
                body=[
                    doc,
                    genereate_sync_call(
                        definition,
                        o,
                        client_schema,
                        config,
                        plugin_config,
                        registry,
                        collapse,
                    )
                    if definition.type != OperationType.SUBSCRIPTION
                    else genereate_sync_iterator(
                        definition,
                        o,
                        client_schema,
                        config,
                        plugin_config,
                        registry,
                        collapse,
                    ),
                ],
                decorator_list=[],
                returns=return_type
                if definition.type != OperationType.SUBSCRIPTION
                else ast.Subscript(
                    value=ast.Name(id="Iterator", ctx=ast.Load()), slice=return_type
                ),
            )
        )

    return tree


class FuncsPlugin(Plugin):
    """This plugin generates functions for each operation in the schema.

    Contratry to the `operations` plugin, this plugin generates real python function
    with type annotations and docstrings according to the operation definition.

    These functions the can be used to call a proxy function (specified through the config)
    to execute the operation.

    You can also specify a list of extra arguments and keyword arguments that will be passed to the proxy function.

    Please consult the examples for more information.

    Example:

    ```python

    async def aexecute(operation: Model, variables: Dict[str, Any], client = None):
        client = client # is the grahql client that can be passed as an extra argument (or retrieved from a contextvar)
        x = await client.aquery(
            operation.Meta.document, operation.Arguments(**variables).dict(by_alias=True)
        )# is the proxy function that will be called (u can validate the variables here)
        return operation(**x.data) # Serialize the result

    ```

    Subscriptions are supported and will map to an async iterator.


    """

    config: FuncsPluginConfig = Field(default_factory=FuncsPluginConfig)

    def generate_ast(
        self,
        client_schema: GraphQLSchema,
        config: GeneratorConfig,
        registry: ClassRegistry,
    ) -> List[ast.AST]:

        plugin_tree = []

        try:
            documents = parse_documents(
                client_schema, self.config.funcs_glob or config.documents
            )
        except NoDocumentsFoundError as e:
            logger.exception(e)
            return plugin_tree

        operations = [
            node
            for node in documents.definitions
            if isinstance(node, OperationDefinitionNode)
        ]

        for operation in operations:
            for definition in get_definitions_for_onode(operation, self.config):
                plugin_tree += generate_operation_func(
                    definition,
                    operation,
                    client_schema,
                    config,
                    self.config,
                    registry,
                )

        return plugin_tree
