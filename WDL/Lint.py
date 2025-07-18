# pylint: disable=protected-access
"""
Annotate WDL document AST with hygiene warnings (underlies ``miniwdl check``)

Given a ``doc: WDL.Document``, the lint warnings can be retrieved like so::

    import WDL
    import WDL.Lint

    lint = WDL.Lint.collect(WDL.Lint.lint(doc, descend_imports=False))
    for (pos, lint_class, message, suppressed) in lint:
        assert isinstance(pos, WDL.SourcePosition)
        assert isinstance(lint_class, str) and isinstance(message, str)
        if not suppressed:
            print(json.dumps({
                "uri"        : pos.uri,
                "abspath"    : pos.abspath,
                "line"       : pos.line,
                "end_line"   : pos.end_line,
                "column"     : pos.column,
                "end_column" : pos.end_column,
                "lint"       : lint_class,
                "message"    : message,
            }))

The ``descend_imports`` flag controls whether lint warnings are generated for imported documents
recursively (true, default), or otherwise only the given document (false).
"""

import subprocess
import tempfile
import json
import os
import random
import shutil
from enum import Enum, auto
from typing import Any, Optional, Union, List
import regex
from . import Error, Type, Env, Expr, Tree, StdLib, Walker, _util


class LintSeverity(Enum):
    """Severity levels for lint findings"""

    MINOR = auto()  # Style suggestions
    MODERATE = auto()  # Potential issues
    MAJOR = auto()  # Serious problems
    CRITICAL = auto()  # Fatal flaws


class LintCategory(Enum):
    """Categories for linters"""

    STYLE = auto()  # Code style and formatting
    SECURITY = auto()  # Security concerns
    PERFORMANCE = auto()  # Performance optimizations
    CORRECTNESS = auto()  # Logical correctness
    PORTABILITY = auto()  # Cross-platform compatibility
    BEST_PRACTICE = auto()  # General best practices
    OTHER = auto()  # Miscellaneous


def _find_doc(obj: Error.SourceNode):
    "find the containing document"
    doc = obj
    while not isinstance(doc, Tree.Document):
        if hasattr(doc, "_doc4lint"):
            doc = getattr(doc, "_doc4lint")
        else:
            doc = getattr(doc, "parent")
        assert doc
    setattr(obj, "_doc4lint", doc)
    return doc


def _find_expr_parent(obj: Expr.Base):
    "find closest ancestor of obj that isn't an expression"
    pt = obj
    while isinstance(pt, Expr.Base):
        pt = getattr(pt, "parent")
    assert pt
    return pt


class Linter(Walker.Base):
    """
    Linters are Walkers which annotate each Tree node with
        ``lint : List[Tuple[SourceNode,str,str,LintSeverity]]``
    providing lint warnings with a node (possibly more-specific than the
    node it's attached to), short codename, message, and severity level.

    Linters initialize the base Walker with ``auto_descend=True`` by default,
    but this can be overridden if control of recursive descent is needed.
    """

    # Default category and severity for the linter
    category = LintCategory.OTHER
    default_severity = LintSeverity.MODERATE

    def __init__(self, auto_descend: bool = True, descend_imports: bool = True):
        super().__init__(auto_descend=auto_descend, descend_imports=descend_imports)

    def add(
        self,
        obj: Error.SourceNode,
        message: str,
        pos: Optional[Error.SourcePosition] = None,
        severity: Optional[LintSeverity] = None,
    ) -> bool:
        """
        Used by subclasses to attach lint to a node.

        Note, lint attaches to Tree nodes (Decl, Task, Workflow, Scatter,
        Conditional, Document). Warnings about individual expressions will
        attach to their parent Tree node.

        Args:
            obj: The AST node to attach the lint warning to
            message: The warning message
            pos: Optional source position (defaults to obj.pos)
            severity: Optional severity level (defaults to the linter's default_severity)
        """
        if isinstance(obj, Expr.Base):
            obj = _find_expr_parent(obj)
        if pos is None:
            pos = obj.pos
        if severity is None:
            severity = self.default_severity

        # check for suppressive comments
        suppress = False
        doc = _find_doc(obj)
        for L in [pos.line, pos.end_line]:
            # check the current line
            comment = doc.source_comments[L - 1]
            if comment and ("!" + self.__class__.__name__) in comment.text:
                suppress = True
            # check the following line if it has nothing but a comment
            comment = doc.source_comments[L] if L < len(doc.source_comments) else None
            if (
                comment
                and ("!" + self.__class__.__name__) in comment.text
                and comment.text.strip() == doc.source_lines[L].strip()
            ):
                suppress = True

        # Create a new lint finding
        new_finding = (pos, self.__class__.__name__, message, suppress, severity)

        # Initialize lint attribute if it doesn't exist
        if not hasattr(obj, "lint"):
            setattr(obj, "lint", [])

        # Check if this exact finding already exists to avoid duplicates
        existing_findings = getattr(obj, "lint")
        for existing in existing_findings:
            existing_pos, existing_cls, existing_msg, _, _ = existing
            if (
                existing_pos.line == pos.line
                and existing_pos.column == pos.column
                and existing_cls == self.__class__.__name__
                and existing_msg == message
            ):
                # This exact finding already exists, don't add it again
                return True

        # Add the new finding
        existing_findings.append(new_finding)
        return True


_all_linters = []


def a_linter(cls):
    """
    Decorator for subclasses of ``Linter`` to register them for use
    """
    _all_linters.append(cls)


def lint(
    doc,
    descend_imports: bool = True,
    additional_linters: Optional[List[str]] = None,
    disabled_linters: Optional[List[str]] = None,
    enabled_categories: Optional[List[str]] = None,
    disabled_categories: Optional[List[str]] = None,
):
    """
    Apply all linters to the document

    Args:
        doc: WDL document to lint
        descend_imports: Whether to lint imported documents
        additional_linters: List of additional linter specifications to load
        disabled_linters: List of linter names to disable
        enabled_categories: List of linter categories to enable
        disabled_categories: List of linter categories to disable
    """
    import logging

    _logger = logging.getLogger("wdl.lint")

    # Add additional markups to the AST for use by the linters
    Walker.SetParents()(doc)
    Walker.MarkCalled()(doc)
    Walker.Multi([Walker.MarkImportsUsed(), Walker.SetReferrers()])(doc)

    # Get configuration
    try:
        from WDL.runtime.config import Loader

        cfg = Loader(_logger)

        # Import here to avoid circular imports
        from .LintPlugins.config import (
            get_additional_linters,
            get_disabled_linters,
            get_enabled_categories,
            get_disabled_categories,
        )

        # Get configuration values, with command-line arguments taking precedence
        additional_linters_cfg = additional_linters or get_additional_linters(cfg)
        disabled_linters_cfg = disabled_linters or get_disabled_linters(cfg)
        enabled_categories_cfg = enabled_categories or get_enabled_categories(cfg)
        disabled_categories_cfg = disabled_categories or get_disabled_categories(cfg)
    except ImportError:
        # Fall back to command-line arguments if configuration module is not available
        additional_linters_cfg = additional_linters or []
        disabled_linters_cfg = disabled_linters or []
        enabled_categories_cfg = enabled_categories or []
        disabled_categories_cfg = disabled_categories or []

    # Get linter classes using the discovery mechanism
    try:
        from .LintPlugins.plugins import discover_linters

        linter_classes = discover_linters(
            additional_linters=additional_linters_cfg,
            disabled_linters=disabled_linters_cfg,
            enabled_categories=enabled_categories_cfg,
            disabled_categories=disabled_categories_cfg,
        )
    except ImportError:
        # Fall back to built-in linters if the plugins module is not available
        linter_classes = _all_linters

    # Instantiate linters
    linter_instances = []
    for linter_class in linter_classes:
        try:
            linter_instances.append(linter_class(descend_imports=descend_imports))
        except Exception as e:
            _logger.warning(f"Failed to instantiate linter {linter_class.__name__}: {str(e)}")

    # Run auto-descend linters with error handling
    auto_descend_linters = [linter for linter in linter_instances if linter.auto_descend]
    if auto_descend_linters:
        try:
            from .LintPlugins.safe_walker import SafeLinterWalker

            SafeLinterWalker(auto_descend_linters, descend_imports=descend_imports)(doc)
        except ImportError:
            # Fall back to standard Walker.Multi if SafeLinterWalker is not available
            try:
                Walker.Multi(auto_descend_linters, descend_imports=descend_imports)(doc)
            except Exception as e:
                _logger.warning(f"Error during auto-descend linting: {str(e)}")

    # Run each non-auto-descend linter with error handling
    for linter in linter_instances:
        if not linter.auto_descend:
            try:
                linter(doc)
            except Exception as e:
                _logger.warning(f"Error running linter {linter.__class__.__name__}: {str(e)}")

    return doc


class _Collector(Walker.Base):
    def __init__(self):
        super().__init__(auto_descend=True)
        self.lint = []

    def __call__(self, obj, descend: Optional[bool] = None):
        if hasattr(obj, "lint"):
            self.lint.extend(getattr(obj, "lint"))
        super().__call__(obj, descend=descend)


def collect(doc):
    """
    Recursively traverse the already-linted document and collect a flat list of
    (SourcePosition, linter_class, message, suppressed)

    Each lint item contains:
    - SourcePosition: Location in the source code
    - linter_class: Name of the linter class that generated the warning
    - message: Warning message
    - suppressed: Boolean indicating if the warning was suppressed

    Note: Severity information is stored in the AST nodes but not included in the
    returned tuples for backward compatibility.
    """
    collector = _Collector()
    collector(doc)

    # Return all findings without global deduplication to maintain backward compatibility
    # The per-node deduplication in Linter.add() is sufficient to prevent duplicates
    # on the same node while preserving distinct findings across different nodes
    return [(pos, cls, msg, suppressed) for pos, cls, msg, suppressed, _ in collector.lint]


def _find_input_decl(obj: Tree.Call, name: str) -> Tree.Decl:
    assert isinstance(obj.callee, (Tree.Task, Tree.Workflow))
    return obj.callee.available_inputs[name]


def _compound_coercion(
    to_type, from_type, base_to_type=Type.Any, from_type_predicate=None, predicates=None
):
    # helper for StringCoercion and FileCoercion to detect coercions implied
    # within compound types like arrays
    # TODO: scheme to target error messages to specific offending type parameter (especially for
    # struct fields and nested structs)
    kwargs = {
        "base_to_type": base_to_type,
        "from_type_predicate": from_type_predicate,
        "predicates": predicates,
    }
    if isinstance(to_type, Type.Array) and isinstance(from_type, Type.Array):
        return _compound_coercion(to_type.item_type, from_type.item_type, **kwargs)
    if isinstance(to_type, Type.Map) and isinstance(from_type, Type.Map):
        return _compound_coercion(
            to_type.item_type[0], from_type.item_type[0], **kwargs
        ) or _compound_coercion(to_type.item_type[1], from_type.item_type[1], **kwargs)
    if isinstance(to_type, Type.Pair) and isinstance(from_type, Type.Pair):
        return _compound_coercion(
            to_type.left_type, from_type.left_type, **kwargs
        ) or _compound_coercion(to_type.right_type, from_type.right_type, **kwargs)
    if (
        isinstance(to_type, Type.StructInstance)
        and to_type.members
        and isinstance(from_type, Type.Object)
    ):
        for field, field_type in from_type.members.items():
            if _compound_coercion(to_type.members[field], field_type, **kwargs):
                return True
        return False
    if isinstance(to_type, base_to_type):
        if predicates:
            return predicates(to_type, from_type)
        if not from_type_predicate:
            from_type_predicate = lambda ty: not isinstance(  # noqa: E731
                ty, (base_to_type, Type.Any)
            )
        return from_type_predicate(from_type)
    return False


def _parent_executable(obj: Error.SourceNode) -> Optional[Union[Tree.Task, Tree.Workflow]]:
    if isinstance(obj, (Tree.Task, Tree.Workflow)):
        return obj
    if hasattr(obj, "parent_executable"):
        return getattr(obj, "parent_executable")
    if hasattr(obj, "parent"):
        ans = _parent_executable(getattr(obj, "parent"))
        setattr(obj, "parent_executable", ans)
        return ans
    return None


@a_linter
class StringCoercion(Linter):
    # String declaration with non-String rhs expression
    # File-to-String coercions are normal in tasks, but flagged at the workflow level.

    category = LintCategory.CORRECTNESS
    default_severity = LintSeverity.MODERATE

    def decl(self, obj: Tree.Decl) -> Any:
        if obj.expr and _compound_coercion(
            obj.type,
            obj.expr.type,
            (Type.String,),
            lambda from_type: not isinstance(
                from_type,
                (
                    (Type.Any, Type.String, Type.File, Type.Directory)
                    if isinstance(_parent_executable(obj), Tree.Task)
                    else (Type.Any, Type.String)
                ),
            ),
        ):
            self.add(obj, "{} {} = :{}:".format(str(obj.type), obj.name, str(obj.expr.type)))

    def expr(self, obj: Expr.Base) -> Any:
        if isinstance(obj, Expr.Apply):
            # String function operands with non-String expression
            if obj.function_name in ("_add", "_interpolation_add"):
                # TODO: should this apply to _interpolation_add, where coercion to String is
                # "obviously" intended?
                any_string = False
                any_string_literal = False
                non_string = None
                for arg in obj.arguments:
                    if isinstance(arg.type, Type.String):
                        any_string = True
                        if isinstance(arg, Expr.String):
                            any_string_literal = True
                    elif not isinstance(arg.type, (Type.File, Type.Directory)):
                        non_string = arg.type
                if any_string and non_string:
                    allowed = _find_doc(obj).effective_wdl_version in ("draft-2", "1.0")
                    if not allowed:
                        self.add(
                            obj,
                            "use interpolation instead of concatenating :String:"
                            f" + :{non_string}: [deprecated in WDL >=1.1]",
                            obj.pos,
                        )
                    elif not any_string_literal and obj.function_name != "_interpolation_add":
                        # Prior to WDL 1.1, + could implicitly coerce a non-String argument to
                        # concatenate with a String argument. Warn about this unless one side is a
                        # a String literal or we're inside an interpolation (as those cases make
                        # the intent clear)
                        self.add(
                            obj,
                            f"consider interpolation instead of concatenating :String: + :{non_string}:",
                            obj.pos,
                        )
            else:
                F = getattr(
                    StdLib.TaskOutputs(_find_doc(obj).effective_wdl_version), obj.function_name
                )
                if isinstance(F, StdLib.StaticFunction) and obj.function_name not in (
                    "basename",  # ok to take either String or File
                    "write_lines",  # clear intent
                    "write_tsv",  # clear intent
                ):
                    for i in range(min(len(F.argument_types), len(obj.arguments))):
                        F_i = F.argument_types[i]
                        arg_i = obj.arguments[i]
                        if _compound_coercion(
                            F_i,
                            arg_i.type,
                            (Type.String,),
                            lambda from_type: not isinstance(
                                from_type,
                                (
                                    (
                                        Type.Any,
                                        Type.String,
                                        Type.File,
                                        Type.Directory,
                                    )
                                    if isinstance(_parent_executable(obj), Tree.Task)
                                    else (Type.Any, Type.String)
                                ),
                            ),
                        ):
                            msg = "{} argument of {}() = :{}:".format(
                                str(F_i), F.name, str(arg_i.type)
                            )
                            self.add(obj, msg, arg_i.pos)
        elif isinstance(obj, Expr.Array):
            # Array literal with mixed item types, one of which is String,
            # causing coercion of the others
            any_string = False
            all_string = True
            item_types = []
            for elt in obj.items:
                if isinstance(elt.type, Type.String):
                    any_string = True
                elif not isinstance(elt.type, (Type.File, Type.Directory, Type.Any)):
                    all_string = False
                item_types.append(str(elt.type))
            if any_string and not all_string:
                msg = "{} literal = [{}]".format(
                    str(obj.type), ", ".join(":{}:".format(ty) for ty in item_types)
                )
                self.add(obj, msg, obj.pos)

    def call(self, obj: Tree.Call) -> Any:
        for name, inp_expr in obj.inputs.items():
            decl = _find_input_decl(obj, name)
            if _compound_coercion(decl.type, inp_expr.type, (Type.String,)):
                msg = "input {} {} = :{}:".format(str(decl.type), decl.name, str(inp_expr.type))
                self.add(obj, msg, inp_expr.pos)


@a_linter
class FileCoercion(Linter):
    # String-to-File coercions are typical in task outputs, but potentially
    # problematic elsewhere.

    category = LintCategory.CORRECTNESS
    default_severity = LintSeverity.MODERATE

    def __init__(self, descend_imports: bool = True):
        super().__init__(auto_descend=False, descend_imports=descend_imports)

    def task(self, obj: Tree.Task) -> Any:
        # descend into everything but outputs
        for d in obj.inputs or []:
            self(d)
        for d in obj.postinputs:
            self(d)
        self(obj.command)
        for _, ex in obj.runtime.items():
            self(ex)

    # File declaration with String rhs expression
    # exception: when rhs looks like a URI constant (typically a default reference database)
    def decl(self, obj: Tree.Decl) -> Any:
        super().decl(obj)
        if obj.expr and _compound_coercion(obj.type, obj.expr.type, (Type.File, Type.Directory)):
            if (
                isinstance(obj.expr, Expr.String)
                and obj.expr.literal
                and "://" in obj.expr.literal.value
            ):
                self.add(
                    obj,
                    f'{obj.type} {obj.name} = "URI" may work with miniwdl, but for WDL portability,'
                    " provide default URI in inputs JSON file",
                )
            else:
                self.add(obj, "{} {} = :{}:".format(str(obj.type), obj.name, str(obj.expr.type)))

    def expr(self, obj: Expr.Base) -> Any:
        super().expr(obj)
        if isinstance(obj, Expr.Apply):
            # File function operands with String expression
            F = getattr(StdLib.TaskOutputs(_find_doc(obj).effective_wdl_version), obj.function_name)
            if isinstance(F, StdLib.StaticFunction):
                for i in range(min(len(F.argument_types), len(obj.arguments))):
                    F_i = F.argument_types[i]
                    arg_i = obj.arguments[i]
                    if _compound_coercion(F_i, arg_i.type, (Type.File, Type.Directory)):
                        msg = "{} argument of {}() = :{}:".format(str(F_i), F.name, str(arg_i.type))
                        self.add(obj, msg, arg_i.pos)
            elif obj.function_name == "size":
                arg0ty = obj.arguments[0].type
                if not isinstance(arg0ty, Type.File) and not (
                    isinstance(arg0ty, Type.Array) and isinstance(arg0ty.item_type, Type.File)
                ):
                    self.add(
                        obj,
                        "File?/Array[File?] argument of size() = :{}:".format(
                            str(obj.arguments[0].type)
                        ),
                        obj.arguments[0].pos,
                    )

    def call(self, obj: Tree.Call) -> Any:
        super().call(obj)
        for name, inp_expr in obj.inputs.items():
            decl = _find_input_decl(obj, name)
            if _compound_coercion(decl.type, inp_expr.type, (Type.File, Type.Directory)):
                msg = "input {} {} = :{}:".format(str(decl.type), decl.name, str(inp_expr.type))
                self.add(obj, msg, inp_expr.pos)


def _array_levels(ty: Type.Base, l=0):
    if isinstance(ty, Type.Array):
        return _array_levels(ty.item_type, l + 1)
    return l


def _is_array_coercion(value_type: Type.Base, expr_type: Type.Base):
    return (
        isinstance(value_type, Type.Array)
        and _array_levels(value_type) > _array_levels(expr_type)
        and not isinstance(expr_type, Type.Any)
        and expr_type != Type.Array(Type.Any())
    )


@a_linter
class ArrayCoercion(Linter):
    # implicit promotion of T to Array[T]
    def decl(self, obj: Tree.Decl) -> Any:
        if obj.expr and _is_array_coercion(obj.type, obj.expr.type):
            msg = "{} {} = :{}:".format(str(obj.type), obj.name, str(obj.expr.type))
            self.add(obj, msg)

    def expr(self, obj: Expr.Base) -> Any:
        if isinstance(obj, Expr.Apply):
            F = getattr(StdLib.TaskOutputs(_find_doc(obj).effective_wdl_version), obj.function_name)
            if isinstance(F, StdLib.StaticFunction):
                for i in range(min(len(F.argument_types), len(obj.arguments))):
                    F_i = F.argument_types[i]
                    arg_i = obj.arguments[i]
                    if _is_array_coercion(F_i, arg_i.type):
                        msg = "{} argument of {}() = :{}:".format(str(F_i), F.name, str(arg_i.type))
                        self.add(obj, msg, arg_i.pos)

    def call(self, obj: Tree.Call) -> Any:
        for name, inp_expr in obj.inputs.items():
            decl = _find_input_decl(obj, name)
            if _is_array_coercion(decl.type, inp_expr.type):
                msg = "input {} {} = :{}:".format(str(decl.type), decl.name, str(inp_expr.type))
                self.add(obj, msg, inp_expr.pos)


@a_linter
class UnverifiedStruct(Linter):
    # non-statically-verified initialization of StructInstance from Map[String,Any]

    def decl(self, obj: Tree.Decl) -> Any:
        if obj.expr and _compound_coercion(
            obj.type,
            obj.expr.type,
            (Type.StructInstance,),
            lambda from_type: isinstance(from_type, Type.Any)
            or (isinstance(from_type, Type.Map) and from_type.literal_keys is None),
        ):
            self.add(
                obj,
                "{} {} = :{}: -- struct initializer isn't statically verified".format(
                    str(obj.type), obj.name, str(obj.expr.type)
                ),
            )

    def call(self, obj: Tree.Call) -> Any:
        for name, inp_expr in obj.inputs.items():
            decl = _find_input_decl(obj, name)
            if _compound_coercion(
                decl.type,
                inp_expr.type,
                (Type.StructInstance,),
                lambda from_type: isinstance(from_type, Type.Any)
                or (isinstance(from_type, Type.Map) and from_type.literal_keys is None),
            ):
                msg = "input {} {} = :{}: -- struct initializer isn't statically verified".format(
                    str(decl.type), decl.name, str(inp_expr.type)
                )
                self.add(obj, msg, inp_expr.pos)


@a_linter
class OptionalCoercion(Linter):
    # Expression of optional type where a non-optional value is expected
    # Normally these fail typechecking, but the enforcement isn't stringent in
    # older WDLs.
    # TODO: suppress within 'if (defined(x))' consequent
    def expr(self, obj: Expr.Base) -> Any:
        if isinstance(obj, Expr.Apply):
            if obj.function_name in ["_add", "_sub", "_mul", "_div", "_land", "_lor"]:
                # excluded _interpolation_add, since interpolations expressly allow this
                assert len(obj.arguments) == 2
                arg0ty = obj.arguments[0].type
                arg1ty = obj.arguments[1].type
                if arg0ty.optional or arg1ty.optional:
                    self.add(
                        obj,
                        "infix operator has :{}: and :{}: operands".format(
                            str(arg0ty), str(arg1ty)
                        ),
                        obj.pos,
                    )
            else:
                F = getattr(
                    StdLib.TaskOutputs(_find_doc(obj).effective_wdl_version), obj.function_name
                )
                if isinstance(F, StdLib.StaticFunction):
                    for i in range(min(len(F.argument_types), len(obj.arguments))):
                        F_i = F.argument_types[i]
                        arg_i = obj.arguments[i]
                        if not arg_i.type.coerces(F_i, check_quant=True) and not _is_array_coercion(
                            F_i, arg_i.type
                        ):
                            msg = "{} argument of {}() = :{}:".format(
                                str(F.argument_types[i]), F.name, str(obj.arguments[i].type)
                            )
                            self.add(obj, msg, obj.arguments[i].pos)

    def decl(self, obj: Tree.Decl) -> Any:
        if (
            obj.expr
            and not obj.expr.type.coerces(obj.type, check_quant=True)
            and not _is_array_coercion(obj.type, obj.expr.type)
        ):
            self.add(obj, "{} {} = :{}:".format(str(obj.type), obj.name, str(obj.expr.type)))

    def call(self, obj: Tree.Call) -> Any:
        for name, inp_expr in obj.inputs.items():
            decl = _find_input_decl(obj, name)
            # treat input with default as optional, with or without the ? type quantifier
            decltype = decl.type.copy(optional=True) if decl.expr else decl.type
            if not inp_expr.type.coerces(decltype, check_quant=True) and not _is_array_coercion(
                decltype, inp_expr.type
            ):
                msg = "input {} {} = :{}:".format(str(decl.type), decl.name, str(inp_expr.type))
                self.add(obj, msg, inp_expr.pos)


def _is_nonempty_coercion(value_type: Type.Base, expr_type: Type.Base):
    return (
        isinstance(value_type, Type.Array)
        and isinstance(expr_type, Type.Array)
        and value_type.nonempty
        and not expr_type.nonempty
    )
    # TODO: descend into compound types


@a_linter
class NonemptyCoercion(Linter):
    # An array of possibly-empty type where a nonempty array is expected
    def expr(self, obj: Expr.Base) -> Any:
        if isinstance(obj, Expr.Apply):
            F = getattr(StdLib.TaskOutputs(_find_doc(obj).effective_wdl_version), obj.function_name)
            if isinstance(F, StdLib.StaticFunction):
                for i in range(min(len(F.argument_types), len(obj.arguments))):
                    F_i = F.argument_types[i]
                    arg_i = obj.arguments[i]
                    if _is_nonempty_coercion(F_i, arg_i.type):
                        msg = "{} argument of {}() = :{}:".format(
                            str(F.argument_types[i]), F.name, str(obj.arguments[i].type)
                        )
                        self.add(obj, msg, obj.arguments[i].pos)

    def decl(self, obj: Tree.Decl) -> Any:
        # heuristic exception for: Array[File]+ outp = glob(...)
        if (
            obj.expr
            and _is_nonempty_coercion(obj.type, obj.expr.type)
            and not (
                isinstance(obj.expr, Expr.Apply)
                and obj.expr.function_name in ["glob", "read_lines", "read_tsv", "read_array"]
            )
        ):
            self.add(obj, "{} {} = :{}:".format(str(obj.type), obj.name, str(obj.expr.type)))

    def call(self, obj: Tree.Call) -> Any:
        for name, inp_expr in obj.inputs.items():
            decl = _find_input_decl(obj, name)
            if _is_nonempty_coercion(decl.type, inp_expr.type):
                msg = "input {} {} = :{}:".format(str(decl.type), decl.name, str(inp_expr.type))
                self.add(obj, msg, inp_expr.pos)


@a_linter
class IncompleteCall(Linter):
    # Call without all required inputs (allowed for top-level workflow)
    def call(self, obj: Tree.Call) -> Any:
        assert obj.callee is not None
        required_inputs = set(decl.name for decl in obj.callee.required_inputs)
        for name, _ in obj.inputs.items():
            if name in required_inputs:
                required_inputs.remove(name)
        if required_inputs:
            msg = "required input(s) omitted in call to {} ({})".format(
                obj.callee.name, ", ".join(required_inputs)
            )
            self.add(obj, msg)


@a_linter
class NameCollision(Linter):
    # Name collisions between
    # - call and import
    # - call and struct type/alias
    # - decl and import
    # - decl and workflow
    # - decl and task
    # - decl and struct type/alias
    # - scatter variable and import
    # - scatter variable and workflow
    # - scatter variable and task
    # - scatter variable and struct type/alias
    # - workflow and import
    # - workflow and struct type/alias
    # - task and import
    # - task and struct type/alias
    # - struct type/alias and import
    # These are allowed, but confusing.
    def call(self, obj: Tree.Call) -> Any:
        doc = _find_doc(obj)
        for imp in doc.imports:
            if imp.namespace == obj.name:
                msg = "call name '{}' collides with imported document namespace".format(obj.name)
                self.add(obj, msg)
        for stb in doc.struct_typedefs:
            assert isinstance(stb, Env.Binding) and isinstance(stb.value, Tree.StructTypeDef)
            if stb.name == obj.name:
                msg = "call name '{}' collides with {}struct type".format(
                    obj.name, "imported " if stb.value.imported else ""
                )
                self.add(obj, msg)

    def decl(self, obj: Tree.Decl) -> Any:
        doc = _find_doc(obj)
        assert isinstance(doc, Tree.Document)
        for imp in doc.imports:
            if imp.namespace == obj.name:
                msg = "declaration of '{}' collides with imported document namespace".format(
                    obj.name
                )
                self.add(obj, msg)
        if doc.workflow and doc.workflow.name == obj.name:
            msg = "declaration of '{}' collides with workflow name".format(obj.name)
            self.add(obj, msg)
        for task in doc.tasks:
            if obj.name == task.name:
                msg = "declaration of '{}' collides with a task name".format(obj.name)
                self.add(obj, msg)
        for stb in doc.struct_typedefs:
            assert isinstance(stb, Env.Binding) and isinstance(stb.value, Tree.StructTypeDef)
            if stb.name == obj.name:
                msg = "declaration of '{}' colides with {}struct type".format(
                    obj.name, "imported " if stb.value.imported else ""
                )
                self.add(obj, msg)

    def scatter(self, obj: Tree.Scatter) -> Any:
        doc = _find_doc(obj)
        for imp in doc.imports:
            if imp.namespace == obj.variable:
                msg = "scatter variable '{}' collides with imported document namespace".format(
                    obj.variable
                )
                self.add(obj, msg)
        if doc.workflow and doc.workflow.name == obj.variable:
            msg = "scatter variable '{}' collides with workflow name".format(obj.variable)
            self.add(obj, msg)
        for task in doc.tasks:
            if obj.variable == task.name:
                msg = "scatter variable '{}' collides with a task name".format(obj.variable)
                self.add(obj, msg)
        for stb in doc.struct_typedefs:
            assert isinstance(stb, Env.Binding) and isinstance(stb.value, Tree.StructTypeDef)
            if stb.name == obj.variable:
                msg = "scatter variable '{}' colides with {}struct type".format(
                    obj.variable, "imported " if stb.value.imported else ""
                )
                self.add(obj, msg)

    def workflow(self, obj: Tree.Workflow) -> Any:
        doc = _find_doc(obj)
        for imp in doc.imports:
            if imp.namespace == obj.name:
                msg = "workflow name '{}' collides with imported document namespace".format(
                    obj.name
                )
                self.add(obj, msg)
        for stb in doc.struct_typedefs:
            assert isinstance(stb, Env.Binding) and isinstance(stb.value, Tree.StructTypeDef)
            if stb.name == obj.name:
                msg = "workflow name '{}' colides with {}struct type".format(
                    obj.name, "imported " if stb.value.imported else ""
                )
                self.add(obj, msg)

    def task(self, obj: Tree.Task) -> Any:
        doc = _find_doc(obj)
        for imp in doc.imports:
            if imp.namespace == obj.name:
                msg = "task name '{}' collides with imported document namespace".format(obj.name)
                self.add(obj, msg)
        for stb in doc.struct_typedefs:
            assert isinstance(stb, Env.Binding) and isinstance(stb.value, Tree.StructTypeDef)
            if stb.name == obj.name:
                msg = "task name '{}' colides with {}struct type".format(
                    obj.name, "imported " if stb.value.imported else ""
                )
                self.add(obj, msg)

    def document(self, obj: Tree.Document) -> Any:
        for imp in obj.imports:
            for stb in obj.struct_typedefs:
                assert isinstance(stb, Env.Binding) and isinstance(stb.value, Tree.StructTypeDef)
                if stb.name == imp.namespace:
                    msg = "imported document namespace '{}' collides with {}struct type".format(
                        imp.namespace, "imported " if stb.value.imported else ""
                    )
                    self.add(obj, msg, imp.pos)


@a_linter
class UnusedImport(Linter):
    # Nothing used from an imported document
    # TODO: clarify confusion when none of an imported document D's structs are used because all
    #       the same struct definitions were imported from a different document E (probably because
    #       E itself imported D)
    def document(self, obj: Tree.Document) -> Any:
        for imp in obj.imports:
            if imp.namespace not in getattr(obj, "imports_used"):
                self.add(
                    obj,
                    "no use of workflow, tasks, or structs defined in the imported document "
                    + imp.namespace,
                    pos=imp.pos,
                )


@a_linter
class ImportNewerWDL(Linter):
    # Document imports a document with a newer WDL version
    def document(self, obj: Tree.Document) -> Any:
        doc_version = self._version_order(obj.effective_wdl_version)
        for imp in obj.imports:
            assert imp.doc
            if self._version_order(imp.doc.effective_wdl_version) > doc_version:
                self.add(
                    obj,
                    "imported document has newer WDL version",
                    pos=imp.pos,
                )

    def _version_order(self, wdl_version: str) -> int:
        if wdl_version == "draft-2":
            return 2
        elif wdl_version == "development":
            return 99
        return int(wdl_version.replace(".", ""))


@a_linter
class ForwardReference(Linter):
    # Ident referencing a value or call output lexically precedes Decl/Call
    def expr(self, obj: Expr.Base) -> Any:
        if isinstance(obj, Expr.Ident):
            referee = obj.referee
            if isinstance(referee, Tree.Gather):
                referee = referee.final_referee
            if referee.pos.line > obj.pos.line or (  # type: ignore
                referee.pos.line == obj.pos.line  # type: ignore
                and referee.pos.column > obj.pos.column  # type: ignore
            ):
                if isinstance(referee, Tree.Decl):
                    msg = "reference to {} precedes its declaration".format(obj.name)
                elif isinstance(referee, Tree.Call):
                    msg = "reference to output {} precedes the call".format(obj.name)
                else:
                    assert False
                self.add(obj, msg, obj.pos)


@a_linter
class UnusedDeclaration(Linter):
    # Nothing references a (non-input) Decl
    def decl(self, obj: Tree.Decl) -> Any:
        pt = getattr(obj, "parent")
        is_output = (
            isinstance(pt, (Tree.Workflow, Tree.Task))
            and getattr(pt, "outputs")
            and obj in getattr(pt, "outputs")
        )
        if not is_output and not getattr(obj, "referrers", []):
            # heuristic exceptions:
            # 1. File whose name suggests it's an hts index file; as these
            #    commonly need to be localized, but not explicitly used in task
            #    command
            # 2. dxWDL "native" task stubs, which declare inputs but leave
            #    command empty.
            # 3. task declaration has "env" decorator and the command uses it
            #    as an environment variable
            index_suffixes = [
                "index",
                "indexes",
                "indices",
                "idx",
                "tbi",
                "bai",
                "crai",
                "csi",
                "fai",
                "dict",
            ]
            if not (
                (
                    isinstance(obj.type, Type.File)
                    and (sum(1 for sfx in index_suffixes if obj.name.lower().endswith(sfx)) > 0)
                )
                or (
                    isinstance(obj.type, Type.Array)
                    and isinstance(obj.type.item_type, Type.File)
                    and (sum(1 for sfx in index_suffixes if obj.name.lower().endswith(sfx)) > 0)
                )
                or (
                    isinstance(pt, Tree.Task)
                    and pt.meta.get("type") == "native"
                    and pt.meta.get("id")
                )
                or self._used_as_command_env_var(obj)
            ):
                self.add(obj, "nothing references {} {}".format(str(obj.type), obj.name))

    def _used_as_command_env_var(self, decl: Tree.Decl) -> bool:
        # Task input declarations with the "env" modifier are intended to be
        # used as environment variables in the task command. False-positive
        # UnusedDeclaration warnings might result because such references are
        # not modeled in our WDL syntax tree.
        # Avoid this by searching for apparent usage of the environment
        # variable in string literal parts of the task command script. This
        # isn't a perfect heuristic (e.g. it could be single-quoted within the
        # script), but that's OK for lint warning purposes.
        task = getattr(decl, "parent")
        if not (isinstance(task, Tree.Task) and decl.decor.get("env", False)):
            return False
        pat = regex.compile(r"\$\{?" + decl.name + r"([^0-9A-Za-z_]|$)")
        for part in task.command.parts:
            if isinstance(part, str):
                if pat.search(part):
                    return True
        return False


@a_linter
class UnusedCall(Linter):
    # the outputs of a Call are neither used nor propagated

    def call(self, obj: Tree.Call) -> Any:
        if obj.effective_outputs and not getattr(obj, "referrers", []):
            workflow = obj
            while not isinstance(workflow, Tree.Workflow):
                workflow = getattr(workflow, "parent")
            assert isinstance(workflow, Tree.Workflow)
            if workflow.outputs is not None:
                self.add(
                    obj,
                    "nothing references the outputs of the call "
                    + obj.name
                    + " nor are are they output from the workflow "
                    + workflow.name,
                )


@a_linter
class UnnecessaryQuantifier(Linter):
    # A declaration like T? x = :T: where the right-hand side can't be null.
    # Caveats:
    # 1. Exception for File? output of tasks, where this is normal.
    # 2. Specific warning when x is an input, and the interpretation is underspecified by WDL
    #    (called with None, does the binding take None or the default?)
    def decl(self, obj: Tree.Decl) -> Any:
        if obj.type.optional and obj.expr and not obj.expr.type.optional:
            tw = obj
            while not isinstance(tw, (Tree.Task, Tree.Workflow)):
                tw = getattr(tw, "parent")
            assert isinstance(tw, (Tree.Task, Tree.Workflow))
            if not (
                isinstance(tw, Tree.Task)
                and isinstance(obj.type, (Type.File, Type.Directory))
                and obj in tw.outputs
            ):
                if not isinstance(tw.inputs, list) or obj in tw.inputs:
                    self.add(
                        obj,
                        f"input {obj.type} {obj.name} is implicitly optional since it has a default;"
                        " consider removing ? quantifier, which may not behave consistently between WDL interpreters",
                    )
                else:
                    self.add(
                        obj,
                        f"unnecessary optional quantifier (?) for non-input {obj.type} {obj.name}",
                    )


_shellcheck_available = None


@a_linter
class CommandShellCheck(Linter):
    # If ShellCheck is installed, run it on the task command and propagate any
    # additional lint it finds.

    category = LintCategory.CORRECTNESS
    default_severity = LintSeverity.MODERATE

    # we suppress
    #   SC1083 This {/} is literal
    #   SC2043 This loop will only ever run once for a constant value
    #   SC2050 This expression is constant
    #   SC2157 Argument to -n is always true due to literal strings
    #   SC2193 The arguments to this comparison can never be equal
    # which can be triggered by dummy values we substitute to write the script
    # also SC1009 and SC1072 are non-informative commentary
    _suppressions = [1009, 1072, 1083, 2043, 2050, 2157, 2193]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._tmpdir = tempfile.mkdtemp(prefix="miniwdl_shellcheck_")
        global _shellcheck_available
        if _shellcheck_available is None:
            _shellcheck_available = shutil.which("shellcheck") is not None

    def __del__(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def task(self, obj: Tree.Task) -> Any:
        global _shellcheck_available
        if not _shellcheck_available:
            return

        # for each expression placeholder in the command, make up a dummy value
        # of the appropriate type that shouldn't trigger shellcheck
        command = []
        for part in obj.command.parts:
            if isinstance(part, Expr.Placeholder):
                command.append(_shellcheck_dummy_value(part.expr.type, part.pos))
            else:
                assert isinstance(part, str)
                command.append(part)
        col_offset, command_str = _util.strip_leading_whitespace("".join(command))

        # write out a temp file with this fake script
        tfn = os.path.join(self._tmpdir, obj.name)
        with open(tfn, "w") as outfile:
            outfile.write(command_str)

        # run shellcheck on it & collect JSON results
        shellcheck_items = None
        try:
            shellcheck_items = subprocess.check_output(
                [
                    "shellcheck",
                    "-s",
                    "bash",
                    "-f",
                    "json",
                    "-e",
                    ",".join(str(c) for c in self.__class__._suppressions),
                    tfn,
                ]
            )
        except subprocess.CalledProcessError as cpe:
            if cpe.returncode in (0, 1):
                shellcheck_items = cpe.stdout
            else:
                self.add(
                    obj,
                    "shellcheck failed on the task command; update shellcheck version or use --no-shellcheck "
                    "to suppress this warning",
                    obj.command.pos,
                )

        if shellcheck_items:
            env_decls = set(
                decl.name
                for decl in ((obj.inputs or []) + obj.postinputs)
                if decl.decor.get("env", False)
            )
            try:
                shellcheck_items = json.loads(shellcheck_items)
                assert isinstance(shellcheck_items, list)

                # annotate on tree, adding appropriate offsets to line/column positions
                for item in shellcheck_items:
                    if item["code"] == 2154 and item["message"].split(" ")[0] in env_decls:
                        # Suppress SC2154 "var is referenced but not assigned" specifically when
                        # var corresponds to a declaration with the "env" modifier. ShellCheck
                        # doesn't know that command expects this var to be set in its environment.
                        continue
                    line = obj.command.pos.line + item["line"] - 1
                    column = col_offset + item["column"] - 1
                    self.add(
                        obj,
                        "SC{} {}".format(item["code"], item["message"]),
                        Error.SourcePosition(
                            uri=obj.command.pos.uri,
                            abspath=obj.command.pos.abspath,
                            line=line,
                            column=column,
                            end_line=line,
                            end_column=column,
                        ),
                    )
            except Exception:
                self.add(
                    obj,
                    "error parsing shellcheck output JSON; update shellcheck version or use --no-shellcheck "
                    "to suppress this warning",
                    obj.command.pos,
                )


def _shellcheck_dummy_value(ty, pos):
    if isinstance(ty, Type.Array):
        return _shellcheck_dummy_value(ty.item_type, pos)
    if isinstance(ty, Type.Boolean):
        return "false"
    # estimate the length of the interpolation in the original source, so that
    # shellcheck will see the same column numbers. + 3 accounts for "~{" and "}"
    desired_length = max(1, pos.end_column - pos.column) + 3
    if isinstance(ty, (Type.Int, Type.Float)):
        return "4" * desired_length
    # assert ty.coerces(Type.String), str(ty)
    # https://github.com/HumanCellAtlas/skylab/blob/a99b8ddffdb3c0ebdea1a8905d28f01a4d365af5/pipelines/10x/count/count.wdl#L325
    # https://github.com/openwdl/wdl/blob/master/versions/draft-2/SPEC.md#map-serialization
    return "".join(
        chr(ord(random.choice(["A", "a"])) + random.randrange(26)) for _ in range(desired_length)
    )


@a_linter
class MixedIndentation(Linter):
    # Line of task command mixes tab and space indentation

    category = LintCategory.STYLE
    default_severity = LintSeverity.MINOR

    def task(self, obj: Tree.Task) -> Any:
        command_lines = "".join(
            (s if isinstance(s, str) else "$") for s in obj.command.parts
        ).split("\n")
        for ofs, line in enumerate(command_lines):
            indentation = line[: (len(line) - len(line.lstrip()))]
            if " " in indentation and "\t" in indentation:
                self.add(
                    obj,
                    "command indented with both spaces & tabs",
                    Error.SourcePosition(
                        uri=obj.command.pos.uri,
                        abspath=obj.command.pos.abspath,
                        line=obj.command.pos.line + ofs,
                        column=1,
                        end_line=obj.command.pos.line + ofs,
                        end_column=len(line),
                    ),
                )
                break


@a_linter
class SelectArray(Linter):
    # application of select_first or select_all on a non-optional array
    def expr(self, obj: Expr.Base) -> Any:
        if isinstance(obj, Expr.Apply) and obj.function_name in ["select_first", "select_all"]:
            arg0 = obj.arguments[0]
            if isinstance(arg0.type, Type.Array) and not arg0.type.item_type.optional:
                self.add(
                    obj,
                    "array of non-optional items passed to " + obj.function_name,
                    obj.arguments[0].pos,
                )


@a_linter
class UnknownRuntimeKey(Linter):
    # refs:
    # https://cromwell.readthedocs.io/en/develop/RuntimeAttributes/
    # https://github.com/broadinstitute/cromwell/blob/develop/wom/src/main/scala/wom/RuntimeAttributes.scala
    # https://github.com/broadinstitute/cromwell/blob/develop/supportedBackends/google/pipelines/common/src/main/scala/cromwell/backend/google/pipelines/common/PipelinesApiRuntimeAttributes.scala
    # https://github.com/broadinstitute/cromwell/blob/develop/supportedBackends/aws/src/main/scala/cromwell/backend/impl/aws/AwsBatchRuntimeAttributes.scala
    # https://github.com/openwdl/wdl/pull/315
    # https://github.com/dnanexus/dxWDL/blob/master/doc/ExpertOptions.md
    # https://cromwell.readthedocs.io/en/develop/backends/TES/
    # https://aws.github.io/amazon-genomics-cli/docs/workflow-engines/cromwell/#aws-batch-retries

    category = LintCategory.PORTABILITY
    default_severity = LintSeverity.MINOR
    known_keys = set(
        [
            "awsBatchRetryAttempts",
            "bootDiskSizeGb",
            "container",
            "continueOnReturnCode",
            "cpu",
            "cpuPlatform",
            "disk",
            "disks",
            "docker",
            "dockerWorkingDir",
            "dx_instance_type",
            "gpu",
            "gpuCount",
            "gpuType",
            "inlineDockerfile",
            "maxRetries",
            "memory",
            "noAddress",
            "preemptible",
            "queueArn",
            "returnCodes",
            "time",
            "zones",
        ]
    )

    def task(self, obj: Tree.Task) -> Any:
        for k in obj.runtime:
            if k not in self.known_keys:
                self.add(obj, "unknown entry in task runtime section: " + k, obj.runtime[k].pos)


@a_linter
class UnexpectedRuntimeValue(Linter):
    expected = {
        "cpu": (Type.Int, Type.Float, Type.String),
        "memory": (Type.Int, Type.String),
        "docker": (Type.String, Type.Array),
        "gpu": (Type.Boolean,),
    }

    def task(self, obj: Tree.Task) -> Any:
        for k in obj.runtime:
            if not isinstance(obj.runtime[k].type, self.expected.get(k, Type.Base)):
                self.add(
                    obj,
                    f"expected {'/'.join(ty.__name__ for ty in self.expected[k])} for task runtime.{k}",
                    obj.runtime[k].pos,
                )

        if "cpu" in obj.runtime and isinstance(obj.runtime["cpu"].type, Type.String):
            # for historical reasons, allow strings that are int literals, or a single placeholder
            # for an int value
            cpu: Optional[Expr.Base] = obj.runtime["cpu"]
            if isinstance(cpu, Expr.String) and len(cpu.parts) == 3:
                cpu_part = cpu.parts[1]
                if (isinstance(cpu_part, str) and cpu_part.isdigit()) or (
                    isinstance(cpu_part, Expr.Placeholder)
                    and isinstance(cpu_part.expr.type, Type.Int)
                ):
                    cpu = None
            if cpu:
                self.add(obj, "expected Int for task runtime.cpu", cpu.pos)

        if "memory" in obj.runtime:
            memory = obj.runtime["memory"]
            if isinstance(memory, Expr.String) and len(memory.parts) == 3:
                lit = memory.parts[1]
                if isinstance(lit, str):
                    try:
                        _util.parse_byte_size(lit)
                    except Exception:
                        self.add(
                            obj,
                            "runtime.memory doesn't follow expected format like '8G' or '1024 MiB'",
                            memory.pos,
                        )


@a_linter
class MissingVersion(Linter):
    def document(self, obj: Tree.Document) -> Any:
        first_sloc = next(
            (
                p
                for p in enumerate(line.lstrip() for line in obj.source_lines)
                if p[1] and p[1][0] != "#"
            ),
            None,
        )
        # (don't bother with this warning if the document is effectively empty)
        if first_sloc and obj.wdl_version is None:
            line = (first_sloc[0] + 1) if first_sloc else obj.pos.line
            self.add(
                obj,
                "document should declare WDL version; draft-2 assumed",
                Error.SourcePosition(
                    uri=obj.pos.uri,
                    abspath=obj.pos.abspath,
                    line=line,
                    end_line=line,
                    column=1,
                    end_column=1,
                ),
            )


@a_linter
class UnboundDeclaration(Linter):
    # Unbound declaration outside of input{} section in WDL 1.0+
    def decl(self, obj: Tree.Decl) -> Any:
        if not obj.expr:
            if _find_doc(obj).effective_wdl_version != "draft-2":
                exe = obj
                while not isinstance(exe, (Tree.Task, Tree.Workflow)):
                    exe = getattr(exe, "parent")
                assert isinstance(exe, (Tree.Task, Tree.Workflow))
                if obj not in (exe.inputs or []):
                    self.add(
                        obj,
                        f"{obj.type} {obj.name} should either be in the input section or bound to an expression",
                    )


@a_linter
class Deprecated(Linter):
    def expr(self, obj: Expr.Base) -> Any:
        if (
            isinstance(obj, Expr.Placeholder)
            and obj.options
            and _find_doc(obj).effective_wdl_version not in ("draft-2", "1.0")
        ):
            self.add(
                obj,
                "use sep()/select_first()/if-then-else expressions instead of"
                " sep/default/true/false placeholder options [WDL >= 1.1]",
                obj.pos,
            )
        elif (
            isinstance(obj, Expr.Struct)
            and not obj.struct_type_name
            and _find_doc(obj).effective_wdl_version not in ("draft-2", "1.0")
        ):
            self.add(obj, "replace 'object' with specific struct type [WDL >= 1.1]", obj.pos)


# Testing Framework for Linters


def validate_linter(linter_class, wdl_code, expected_lint=None, expected_count=None, version="1.0"):
    """
    Validate a linter with a WDL code fragment.

    This function provides a convenient way to validate custom linters by:
    1. Creating a temporary WDL document from the provided code
    2. Running the specified linter on the document
    3. Collecting the lint results
    4. Verifying the results match expectations

    Args:
        linter_class: The linter class to validate (must be a subclass of Linter)
        wdl_code: WDL code fragment as a string
        expected_lint: List of expected lint messages (partial matches)
        expected_count: Expected number of lint findings (if expected_lint not provided)
        version: WDL version to use (default: "1.0")

    Returns:
        List of lint results: [(pos, linter_class_name, message, suppressed, severity), ...]

    Raises:
        AssertionError: If the linter results don't match expectations
        ValueError: If the linter_class is not a valid Linter subclass

    Example:
        # Test a linter that should find issues
        validate_linter(
            MyLinter,
            '''
            task bad_task {
              command { echo "hello" }
            }
            ''',
            expected_lint=["Task name should be lowercase"]
        )

        # Test a linter that should find no issues
        validate_linter(
            MyLinter,
            '''
            task good_task {
              command { echo "hello" }
            }
            ''',
            expected_lint=[]
        )
    """
    import tempfile
    import os
    from . import load

    # Validate linter class
    if not (isinstance(linter_class, type) and issubclass(linter_class, Linter)):
        raise ValueError(f"linter_class must be a subclass of Linter, got {linter_class}")

    # Prepare WDL code with version if not already present
    if not wdl_code.strip().startswith("version"):
        wdl_code = f"version {version}\n\n{wdl_code}"

    # Create temporary WDL file
    with tempfile.NamedTemporaryFile(mode="w+", suffix=".wdl", delete=False) as tmp_file:
        tmp_file.write(wdl_code)
        tmp_file.flush()

        try:
            # Load and parse the WDL document
            doc = load(tmp_file.name, path=[])

            # Save original linters and add our test linter
            original_linters = _all_linters.copy()
            test_linters = [linter_class]

            try:
                # Run linting with only our test linter
                _all_linters[:] = test_linters
                lint(doc)

                # Collect lint results
                lint_results = collect(doc)

                # Filter results to only include our test linter
                test_linter_name = linter_class.__name__
                filtered_results = [
                    result for result in lint_results if result[1] == test_linter_name
                ]

                # Remove duplicates based on position and message
                # This can happen when linters are called on multiple AST nodes
                unique_results = []
                seen = set()
                for result in filtered_results:
                    pos, cls, msg, suppressed, severity = result
                    # Create a key based on line, column, and message
                    key = (pos.line if pos else 0, pos.column if pos else 0, msg)
                    if key not in seen:
                        seen.add(key)
                        unique_results.append(result)

                # Verify expectations
                if expected_lint is not None:
                    _verify_expected_lint(unique_results, expected_lint, test_linter_name)
                elif expected_count is not None:
                    if len(unique_results) != expected_count:
                        raise AssertionError(
                            f"Expected {expected_count} lint findings from {test_linter_name}, "
                            f"but got {len(unique_results)}: {[r[2] for r in unique_results]}"
                        )

                return unique_results

            finally:
                # Restore original linters
                _all_linters[:] = original_linters

        finally:
            # Clean up temporary file
            os.unlink(tmp_file.name)


def _verify_expected_lint(lint_results, expected_lint, linter_name):
    """
    Verify that lint results match expected messages.

    Args:
        lint_results: List of lint result tuples
        expected_lint: List of expected message substrings
        linter_name: Name of the linter being tested
    """
    actual_messages = [result[2] for result in lint_results]

    if len(expected_lint) == 0:
        # Expecting no lint findings
        if len(lint_results) > 0:
            raise AssertionError(
                f"Expected no lint findings from {linter_name}, "
                f"but got {len(lint_results)}: {actual_messages}"
            )
        return

    # Check that we have the expected number of findings
    if len(lint_results) != len(expected_lint):
        raise AssertionError(
            f"Expected {len(expected_lint)} lint findings from {linter_name}, "
            f"but got {len(lint_results)}: {actual_messages}"
        )

    # Check that each expected message is found
    for i, expected_msg in enumerate(expected_lint):
        if i >= len(actual_messages):
            raise AssertionError(
                f"Expected lint message '{expected_msg}' not found. "
                f"Got {len(actual_messages)} messages: {actual_messages}"
            )

        actual_msg = actual_messages[i]
        if expected_msg not in actual_msg:
            raise AssertionError(
                f"Expected lint message to contain '{expected_msg}', but got '{actual_msg}'"
            )


def create_test_wdl(
    task_name="test_task", command="echo 'hello'", inputs=None, outputs=None, version="1.0"
):
    """
    Utility function to create test WDL fragments.

    Args:
        task_name: Name of the task
        command: Command to execute
        inputs: Dictionary of input declarations {name: type}
        outputs: Dictionary of output declarations {name: type}
        version: WDL version to use

    Returns:
        WDL code as a string

    Example:
        wdl_code = create_test_wdl(
            task_name="my_task",
            command="echo ~{message}",
            inputs={"message": "String"},
            outputs={"result": "String"}
        )
    """
    lines = [f"version {version}", ""]

    lines.append(f"task {task_name} {{")

    # Add inputs section
    if inputs:
        lines.append("  input {")
        for name, type_str in inputs.items():
            lines.append(f"    {type_str} {name}")
        lines.append("  }")

    # Add command section
    lines.append("  command {")
    lines.append(f"    {command}")
    lines.append("  }")

    # Add outputs section
    if outputs:
        lines.append("  output {")
        for name, type_str in outputs.items():
            lines.append(f"    {type_str} {name} = stdout()")
        lines.append("  }")

    lines.append("}")

    return "\n".join(lines)


def assert_lint_contains(lint_results, expected_message, linter_name=None):
    """
    Assert that lint results contain a message with the expected substring.

    Args:
        lint_results: List of lint result tuples
        expected_message: Expected message substring
        linter_name: Optional linter name to filter by

    Raises:
        AssertionError: If the expected message is not found
    """
    filtered_results = lint_results
    if linter_name:
        filtered_results = [r for r in lint_results if r[1] == linter_name]

    messages = [result[2] for result in filtered_results]

    for message in messages:
        if expected_message in message:
            return

    raise AssertionError(
        f"Expected to find message containing '{expected_message}' "
        f"in lint results, but got: {messages}"
    )


def assert_lint_count(lint_results, expected_count, linter_name=None):
    """
    Assert that lint results have the expected count.

    Args:
        lint_results: List of lint result tuples
        expected_count: Expected number of lint findings
        linter_name: Optional linter name to filter by

    Raises:
        AssertionError: If the count doesn't match
    """
    filtered_results = lint_results
    if linter_name:
        filtered_results = [r for r in lint_results if r[1] == linter_name]

    actual_count = len(filtered_results)
    if actual_count != expected_count:
        messages = [result[2] for result in filtered_results]
        raise AssertionError(
            f"Expected {expected_count} lint findings"
            f"{f' from {linter_name}' if linter_name else ''}, "
            f"but got {actual_count}: {messages}"
        )


def assert_lint_severity(lint_results, expected_severity, linter_name=None):
    """
    Assert that lint results have the expected severity.

    Args:
        lint_results: List of lint result tuples
        expected_severity: Expected LintSeverity enum value
        linter_name: Optional linter name to filter by

    Raises:
        AssertionError: If any result has a different severity
    """
    filtered_results = lint_results
    if linter_name:
        filtered_results = [r for r in lint_results if r[1] == linter_name]

    for pos, cls, msg, suppressed, severity in filtered_results:
        if severity != expected_severity:
            raise AssertionError(
                f"Expected lint finding to have severity {expected_severity.name}, "
                f"but got {severity.name}: {msg}"
            )
