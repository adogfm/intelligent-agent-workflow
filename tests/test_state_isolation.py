"""Reference-path regressions for snapshot and commit isolation."""

from __future__ import annotations

from array import array
from collections import defaultdict, deque
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum, Flag, IntEnum, IntFlag
from fractions import Fraction
from functools import partial
from operator import methodcaller
from types import BuiltinFunctionType, MappingProxyType, ModuleType, SimpleNamespace
import re
import unittest
import weakref

from workflow import (
    AgentResult, BaseAgent, StageStatus, WorkflowEngine, WorkflowExecutionError,
    WorkflowState, generate_workflow_report,
)


@dataclass
class Payload:
    items: list[str] = field(default_factory=list)


@dataclass
class Case:
    value: object
    target: Payload
    mutate: object


def append_reference(reference) -> None:
    reference().items.append("uncommitted")


def append_item(items, value) -> None:
    items.append(value)


def unsafe_case(name: str) -> Case:
    target = Payload()
    call = lambda value: value()
    if name == "closure":
        def value():
            target.items.append("uncommitted")
    elif name == "function default":
        def value(data=target):
            data.items.append("uncommitted")
    elif name == "function keyword default":
        def value(*, data=target):
            data.items.append("uncommitted")
    elif name == "function attributes":
        def value():
            value.data.items.append("uncommitted")
        value.data = target
    elif name == "function globals":
        namespace = {"data": target}
        exec("def callback(): data.items.append('uncommitted')", namespace)
        value = namespace["callback"]
    elif name == "builtin bound method":
        value = target.items.append
        call = lambda value: value("uncommitted")
    elif name == "spoofed builtin receiver":
        class Items(list):
            @property
            def __class__(self):
                return ModuleType
        target.items = Items()
        value = target.items.append
        call = lambda value: value("uncommitted")
    elif name == "spoofed builtin object":
        class Value:
            def __init__(self, items):
                self.items = items

            @property
            def __class__(self):
                return BuiltinFunctionType

            @property
            def __self__(self):
                return re

            def __deepcopy__(self, memo):
                return self
        value = Value(target.items)
        call = lambda value: value.items.append("uncommitted")
    elif name == "regex label":
        class Label(str):
            pass
        label = Label("pattern")
        label.items = target.items
        value = re.compile(label)
        call = lambda value: value.pattern.items.append("uncommitted")
    elif name in {"class value", "classmethod", "inherited class data"}:
        class Value:
            items = target.items

            @classmethod
            def change(cls):
                cls.items.append("uncommitted")
        if name == "class value":
            value = Value
            call = lambda value: value.items.append("uncommitted")
        elif name == "classmethod":
            value = Value.change
        else:
            value = Value()
            call = lambda value: value.items.append("uncommitted")
    elif name in {"private inherited data", "mangled inherited data", "dunder inherited data"}:
        attribute = {
            "private inherited data": "_items",
            "mangled inherited data": "_Parent__items",
            "dunder inherited data": "__items__",
        }[name]

        class Parent:
            pass

        setattr(Parent, attribute, target.items)

        class Value(Parent):
            pass

        value = Value()
        call = lambda value: getattr(value, attribute).append("uncommitted")
    elif name in {"shared exception cause", "shared exception context"}:
        attribute = "__cause__" if name == "shared exception cause" else "__context__"

        class Error(ValueError):
            def __deepcopy__(self, memo):
                copied = type(self)(*deepcopy(self.args, memo))
                memo[id(self)] = copied
                setattr(copied, attribute, getattr(self, attribute))
                return copied

        value = Error("outer")
        setattr(value, attribute, ValueError(target.items))
        call = lambda value: getattr(value, attribute).args[0].append("uncommitted")
    elif name.startswith("enum inherited ") or name == "enum added class data":
        attribute = {
            "enum inherited public data": "items",
            "enum inherited private data": "_items",
            "enum inherited mangled data": "_Mixin__items",
            "enum inherited weakref": "_reference",
            "enum inherited slots": "items",
            "enum added class data": "_items",
        }[name]

        class Mixin:
            pass

        class SlottedMixin:
            __slots__ = ("items",)

            def __init__(self, value):
                self.items = target.items

        if name != "enum added class data" and name != "enum inherited slots":
            setattr(Mixin, attribute, weakref.ref(target) if name == "enum inherited weakref" else target.items)

        class Value(SlottedMixin if name == "enum inherited slots" else Mixin, Enum):
            ITEM = "value"

        if name == "enum added class data":
            setattr(Value, attribute, target.items)
        value = Value.ITEM
        if name == "enum inherited weakref":
            call = lambda value: getattr(value, attribute)().items.append("uncommitted")
        else:
            call = lambda value: getattr(value, attribute).append("uncommitted")
    elif name in {"enum mutable sibling value", "enum mutable sibling attribute", "enum cyclic mutable sibling"}:
        class Value(Enum):
            ITEM = "value"
            OTHER = {"items": target.items} if name == "enum mutable sibling value" else "other"

        if name == "enum cyclic mutable sibling":
            Value.ITEM.peer = Value.OTHER
            Value.OTHER.peer = Value.ITEM
            Value.OTHER.data = target
            call = lambda value: value.peer.data.items.append("uncommitted")
        elif name == "enum mutable sibling attribute":
            Value.OTHER.data = target
            call = lambda value: value.OTHER.data.items.append("uncommitted")
        else:
            call = lambda value: value.OTHER.value["items"].append("uncommitted")
        value = Value.ITEM
    elif name in {"flag cached composite", "intflag cached composite"}:
        class Bits(Flag if name == "flag cached composite" else IntFlag):
            FIRST = 1
            SECOND = 2
            THIRD = 4

        composite = Bits.FIRST | Bits.SECOND
        composite.items = target.items
        value = Bits.FIRST
        call = lambda value: (value | value.SECOND).items.append("uncommitted")
    elif name in {"attribute error obj", "os error filename"}:
        if name == "attribute error obj":
            class Error(AttributeError):
                def __deepcopy__(self, memo):
                    copied = type(self)(*deepcopy(self.args, memo), name=self.name, obj=self.obj)
                    memo[id(self)] = copied
                    return copied

            value = Error("missing", name="items", obj=target.items)
            call = lambda value: value.obj.append("uncommitted")
        else:
            class Label(str):
                pass

            filename = Label("source.py")
            filename.items = target.items

            class Error(OSError):
                def __deepcopy__(self, memo):
                    copied = type(self)(*deepcopy(self.args, memo), self.filename)
                    memo[id(self)] = copied
                    return copied

            value = Error(2, "missing", filename)
            call = lambda value: value.filename.items.append("uncommitted")
    elif name in {"enum generator slots", "enum iterator slots", "object generator slots", "object iterator slots"}:
        class Mixin:
            __slots__ = ((slot for slot in ("items",)) if "generator" in name
                         else iter(("items",)))

            def __init__(self, items):
                self.items = items

        if name.startswith("enum"):
            class Status(Mixin, Enum):
                READY = "ready"

                def __init__(self, value):
                    Mixin.__init__(self, target.items)

            value = Status.READY
        else:
            class Value(Mixin):
                __slots__ = ()

                def __deepcopy__(self, memo):
                    copied = type(self)(self.items)
                    memo[id(self)] = copied
                    return copied

            value = Value(target.items)
        call = lambda value: value.items.append("uncommitted")
    elif name == "shadowed native exception args":
        class Error(ValueError):
            __slots__ = ("args",)

            def __init__(self, payload):
                super().__init__(payload)
                self.args = ("shadowed arguments",)

            def __deepcopy__(self, memo):
                copied = type(self)(*self.__reduce__()[1])
                memo[id(self)] = copied
                return copied

        value = Error(target.items)
        call = lambda value: value.__reduce__()[1][0].append("uncommitted")
    elif name in {"underscore private slot", "underscore enum private slot"}:
        class _:
            __slots__ = ("__items",)

            def __init__(self, items):
                self.__items = items

            def mutate(self):
                self.__items.append("uncommitted")

        if name == "underscore private slot":
            class Value(_):
                def __deepcopy__(self, memo):
                    copied = type(self)(getattr(self, "__items"))
                    memo[id(self)] = copied
                    return copied

            value = Value(target.items)
        else:
            class Status(_, Enum):
                READY = "ready"

                def __init__(self, value):
                    _.__init__(self, target.items)

            value = Status.READY
        call = lambda value: value.mutate()
    elif name in {"mutable enum value", "mutable enum attribute"}:
        if name == "mutable enum value":
            class Value(Enum):
                ITEM = {"items": target.items}
            value = Value.ITEM
            call = lambda value: value.value["items"].append("uncommitted")
        else:
            class Value(Enum):
                ITEM = "value"
            value = Value.ITEM
            value.data = target
            call = lambda value: value.data.items.append("uncommitted")
    elif name in {"copy returns self", "copy shares child", "slot copy shares child"}:
        class Value:
            def __init__(self, items):
                self.items = items

            def __deepcopy__(self, memo):
                return self if name == "copy returns self" else type(self)(self.items)

        class SlottedValue:
            __slots__ = ("items",)

            def __init__(self, items):
                self.items = items

            def __deepcopy__(self, memo):
                return type(self)(self.items)
        value = (SlottedValue if name == "slot copy shares child" else Value)(target.items)
        call = lambda value: value.items.append("uncommitted")
    elif name in {"partial argument", "partial keyword", "partial closure"}:
        reference = weakref.ref(target)
        if name == "partial argument":
            value = partial(append_reference, reference)
        elif name == "partial keyword":
            value = partial(append_reference, reference=reference)
        else:
            def callback():
                target.items.append("uncommitted")
            value = partial(callback)
    elif name in {"method default", "method keyword default", "method closure", "callable closure"}:
        class Receiver:
            def default(self, data=target):
                data.items.append("uncommitted")

            def keyword(self, *, data=target):
                data.items.append("uncommitted")

            def closure(self):
                target.items.append("uncommitted")

            def __call__(self):
                target.items.append("uncommitted")
        receiver = Receiver()
        value = {"method default": receiver.default, "method keyword default": receiver.keyword,
                 "method closure": receiver.closure, "callable closure": receiver}[name]
    elif name == "method globals":
        namespace = {"data": target}
        exec("def callback(self): data.items.append('uncommitted')", namespace)
        class Receiver:
            callback = namespace["callback"]
        value = Receiver().callback
    elif name == "defaultdict factory":
        def factory():
            target.items.append("uncommitted")
            return []
        value = defaultdict(factory)
        call = lambda value: value["missing"]
    elif name in {"iterator", "slice", "exception arguments"}:
        reference = weakref.ref(target)
        if name == "iterator":
            value = iter([reference])
            call = lambda value: append_reference(next(value))
        elif name == "slice":
            value = slice(reference)
            call = lambda value: append_reference(value.stop)
        else:
            value = ValueError(reference)
            call = lambda value: append_reference(value.args[0])
    elif name == "opaque callable":
        class Receiver:
            def change(self, reference):
                append_reference(reference)
        value = methodcaller("change", weakref.ref(target))
        call = lambda value: value(Receiver())
    else:
        raise AssertionError(name)
    return Case(value, target, call)


UNSAFE_CASES = (
    "closure", "function default", "function keyword default", "function attributes", "function globals",
    "builtin bound method", "spoofed builtin receiver", "spoofed builtin object", "regex label",
    "class value", "classmethod", "inherited class data",
    "private inherited data", "mangled inherited data", "dunder inherited data",
    "shared exception cause", "shared exception context",
    "enum inherited public data", "enum inherited private data", "enum inherited mangled data",
    "enum inherited weakref", "enum inherited slots", "enum added class data",
    "enum mutable sibling value", "enum mutable sibling attribute",
    "enum cyclic mutable sibling",
    "flag cached composite", "intflag cached composite", "attribute error obj", "os error filename",
    "enum generator slots", "enum iterator slots", "object generator slots", "object iterator slots",
    "shadowed native exception args", "underscore private slot", "underscore enum private slot",
    "mutable enum value", "mutable enum attribute", "copy returns self", "copy shares child",
    "slot copy shares child", "partial argument", "partial keyword", "partial closure",
    "method default", "method keyword default", "method closure", "method globals", "callable closure",
    "defaultdict factory", "iterator", "slice", "exception arguments", "opaque callable",
)


class ReturningAgent(BaseAgent):
    name = "returning"

    def __init__(self, result: AgentResult):
        self.result = result

    def run(self, state):
        return self.result


class StateReferenceIsolationTests(unittest.TestCase):
    def test_iterable_slot_enum_cannot_modify_source_after_agent_failure(self):
        for name in ("enum generator slots", "enum iterator slots"):
            with self.subTest(case=name):
                case = unsafe_case(name)
                state = WorkflowState({"status": case.value, "payload": case.target})
                calls = []

                class FailingAgent(BaseAgent):
                    name = "iterable-slot-declaration"

                    def run(self, snapshot):
                        calls.append(True)
                        snapshot["status"].items.append("uncommitted")
                        raise ValueError("failed after slot mutation")

                with self.assertRaises(WorkflowExecutionError):
                    WorkflowEngine().add_step("fail", FailingAgent()).run(state)
                self.assertEqual(case.target.items, [])
                self.assertEqual(calls, [])
                self.assertEqual(state.version, 0)
                self.assertEqual(state.history, ())
                self.assertEqual(state.agent_results, ())

    def test_iterable_slot_enum_report_copy_does_not_expose_source_data(self):
        for name in ("enum generator slots", "enum iterator slots"):
            with self.subTest(case=name):
                case = unsafe_case(name)
                state = WorkflowState({"report": case.value, "payload": case.target})
                state.start()
                state.complete()
                report = generate_workflow_report(state)
                if not isinstance(report.final_review_result, str):
                    report.final_review_result.items.append("report mutation")
                self.assertEqual(case.target.items, [])
                self.assertIs(type(report.final_review_result), str)
                self.assertIs(state.get("report"), case.value)

    def test_iterable_slots_with_correct_copy_preserve_aliases_and_cycles(self):
        for style in ("generator", "iterator"):
            with self.subTest(style=style):
                class Parent:
                    __slots__ = ((name for name in ("__items", "optional")) if style == "generator"
                                 else iter(("__items", "optional")))

                    def __init__(self, items):
                        self.__items = items

                    def read(self):
                        return self.__items

                class Value(Parent):
                    __slots__ = ()

                    def __deepcopy__(self, memo):
                        copied = type(self).__new__(type(self))
                        memo[id(self)] = copied
                        Parent.__init__(copied, deepcopy(self.read(), memo))
                        return copied

                items = []
                value = Value(items)
                items.append(value)
                snapshot = WorkflowState({"value": value, "items": items}).snapshot()
                copied = snapshot["value"]
                self.assertIs(copied.read(), snapshot["items"])
                self.assertIs(copied.read()[0], copied)
                copied.read().append("snapshot")
                exported = snapshot.to_dict()
                exported["value"].read().append("export")
                self.assertEqual(items, [value])
                self.assertEqual(snapshot["items"], [copied, "snapshot"])
                self.assertIs(exported["items"][0], exported["value"])
                self.assertEqual(len(exported["items"]), 3)
                with self.assertRaises(AttributeError):
                    copied.optional

    def test_iterable_slot_enum_with_immutable_values_remains_supported(self):
        for style in ("generator", "iterator"):
            with self.subTest(style=style):
                class _:
                    __slots__ = ((name for name in ("items", "__labels")) if style == "generator"
                                 else iter(("items", "__labels")))

                    def __init__(self, items):
                        self.items = items
                        self.__labels = items

                    def labels(self):
                        return self.__labels

                class Status(_, Enum):
                    READY = "ready"
                    OTHER = "other"

                    def __init__(self, value):
                        _.__init__(self, ("immutable",))

                snapshot = WorkflowState({"status": Status.READY}).snapshot()
                self.assertIs(snapshot["status"], Status.READY)
                self.assertEqual(snapshot["status"].labels(), ("immutable",))
                self.assertIs(snapshot["status"].labels(), snapshot["status"].items)
                self.assertIs(snapshot.to_dict()["status"], Status.READY)

    def test_shadowed_exception_args_cannot_modify_state_or_reports(self):
        case = unsafe_case("shadowed native exception args")
        state = WorkflowState({"payload": case.target, "error": case.value})
        calls = []

        class FailingAgent(BaseAgent):
            name = "shadowed-args"

            def run(self, snapshot):
                calls.append(True)
                case.mutate(snapshot["error"])
                raise ValueError("failed after native arguments mutation")

        with self.assertRaises(WorkflowExecutionError):
            WorkflowEngine().add_step("fail", FailingAgent()).run(state)
        self.assertEqual(case.target.items, [])
        self.assertEqual(calls, [])
        self.assertEqual(state.version, 0)
        self.assertEqual(state.history, ())
        self.assertEqual(state.agent_results, ())

        completed = WorkflowState({"report": case.value})
        completed.start()
        completed.complete()
        report = generate_workflow_report(completed)
        self.assertIs(type(report.final_review_result), str)
        self.assertEqual(case.target.items, [])

    def test_shadowed_exception_slots_do_not_hide_native_fields(self):
        try:
            raise ValueError("native traceback")
        except ValueError as caught:
            traceback = caught.__traceback__
        for name in ("args", "__cause__", "__context__", "__traceback__"):
            with self.subTest(field=name):
                items = []
                native_value = ((items,) if name == "args" else
                                traceback if name == "__traceback__" else ValueError(items))
                descriptor = vars(BaseException)[name]

                class Error(ValueError):
                    __slots__ = (name,)

                    def __deepcopy__(self, memo):
                        copied = type(self)("safe")
                        memo[id(self)] = copied
                        descriptor.__set__(copied, descriptor.__get__(self, type(self)))
                        setattr(copied, name, None)
                        return copied

                error = Error("safe")
                descriptor.__set__(error, native_value)
                setattr(error, name, None)
                state = WorkflowState({"value": "original"})
                with self.assertRaises(TypeError):
                    WorkflowState({"error": error}).snapshot()
                with self.assertRaises(TypeError):
                    state.apply({"value": "new", "error": error}, source="prepare")
                self.assertEqual(state.to_dict(), {"value": "original"})
                self.assertEqual(state.version, 0)
                self.assertEqual(state.history, ())
                self.assertEqual(items, [])

    def test_shadowed_exception_native_and_python_slots_are_both_detached(self):
        class Error(ValueError):
            __slots__ = ("args",)

            def __init__(self, native, shadow):
                super().__init__(native)
                self.args = (shadow,)

            def __deepcopy__(self, memo):
                copied = type(self).__new__(type(self))
                memo[id(self)] = copied
                native = BaseException.args.__get__(self, type(self))
                BaseException.args.__set__(copied, deepcopy(native, memo))
                copied.args = deepcopy(self.args, memo)
                return copied

        native, shadow = [], []
        error = Error(native, shadow)
        native.append(error)
        shadow.append(error)
        snapshot = WorkflowState({"error": error, "native": native, "shadow": shadow}).snapshot()
        copied = snapshot["error"]
        copied_native = copied.__reduce__()[1][0]
        copied_shadow = copied.args[0]
        self.assertIs(copied_native, snapshot["native"])
        self.assertIs(copied_shadow, snapshot["shadow"])
        self.assertIs(copied_native[0], copied)
        self.assertIs(copied_shadow[0], copied)
        copied_native.append("snapshot")
        copied_shadow.append("snapshot")
        exported = snapshot.to_dict()
        exported["error"].__reduce__()[1][0].append("export")
        self.assertEqual(native, [error])
        self.assertEqual(shadow, [error])
        self.assertEqual(len(snapshot["native"]), 2)
        self.assertEqual(len(exported["native"]), 3)

    def test_underscore_private_slots_cannot_modify_state_or_reports(self):
        for name in ("underscore private slot", "underscore enum private slot"):
            with self.subTest(case=name):
                case = unsafe_case(name)
                state = WorkflowState({"report": case.value})
                state.start()
                state.complete()
                report = generate_workflow_report(state)
                self.assertIs(type(report.final_review_result), str)
                self.assertEqual(case.target.items, [])
                self.assertIs(state.get("report"), case.value)

    def test_underscore_private_slots_with_safe_data_remain_supported(self):
        class _:
            __slots__ = ("__items",)

            def __init__(self, items):
                self.__items = items

            def read(self):
                return self.__items

        items = []
        value = _(items)
        snapshot = WorkflowState({"value": value, "items": items}).snapshot()
        self.assertIs(snapshot["value"].read(), snapshot["items"])
        snapshot["value"].read().append("snapshot")
        exported = snapshot.to_dict()
        exported["value"].read().append("export")
        self.assertEqual(items, [])
        self.assertEqual(snapshot["items"], ["snapshot"])
        self.assertEqual(exported["items"], ["snapshot", "export"])

        class Status(_, Enum):
            READY = "ready"

            def __init__(self, value):
                _.__init__(self, ("immutable",))

        enum_snapshot = WorkflowState({"status": Status.READY}).snapshot()
        self.assertIs(enum_snapshot["status"], Status.READY)
        self.assertEqual(enum_snapshot["status"].read(), ("immutable",))

    def test_cached_flag_inversion_cycles_remain_supported(self):
        for base in (Flag, IntFlag):
            with self.subTest(kind=base.__name__):
                class Bits(base):
                    FIRST = 1
                    SECOND = 2
                    THIRD = 4
                    FOURTH = 8

                for number in range(16):
                    ~Bits(number)
                state = WorkflowState({"flag": Bits.FIRST})
                snapshot = state.snapshot()
                self.assertIs(snapshot.to_dict()["flag"], Bits.FIRST)
                self.assertIs(~snapshot["flag"], ~Bits.FIRST)
                Bits(0).items = []
                with self.assertRaises(TypeError):
                    state.snapshot()

    def test_cached_flag_member_data_is_rechecked_between_snapshots(self):
        for base in (Flag, IntFlag):
            with self.subTest(kind=base.__name__):
                class Bits(base):
                    FIRST = 1
                    SECOND = 2
                    THIRD = 4

                composite = Bits.FIRST | Bits.SECOND
                state = WorkflowState({"flag": Bits.FIRST})
                self.assertIs(state.snapshot()["flag"] | Bits.SECOND, composite)
                composite.items = []
                with self.assertRaises(TypeError):
                    state.snapshot()
                composite.items = ("immutable",)
                snapshot = state.snapshot()
                self.assertEqual((snapshot["flag"] | Bits.SECOND).items, ("immutable",))
                self.assertIs(snapshot.to_dict()["flag"], Bits.FIRST)

    def test_native_exception_fields_preserve_detached_aliases_and_cycles(self):
        class Label(str):
            pass

        class AttributeFailure(AttributeError):
            def __deepcopy__(self, memo):
                copied = type(self)(*deepcopy(self.args, memo), name=self.name)
                memo[id(self)] = copied
                copied.obj = deepcopy(self.obj, memo)
                return copied

        class FileFailure(OSError):
            def __deepcopy__(self, memo):
                copied = type(self)(*deepcopy(self.args, memo))
                memo[id(self)] = copied
                copied.filename = deepcopy(self.filename, memo)
                return copied

        for kind in ("attribute", "filename"):
            with self.subTest(kind=kind):
                items = []
                if kind == "attribute":
                    error = AttributeFailure("missing", name="items", obj=items)
                    items.append(error)
                else:
                    filename = Label("source.py")
                    filename.items = items
                    error = FileFailure(2, "missing", filename)
                    items.append(error)
                snapshot = WorkflowState({"error": error, "items": items}).snapshot()
                copied = snapshot["error"]
                copied_items = copied.obj if kind == "attribute" else copied.filename.items
                self.assertIs(copied_items, snapshot["items"])
                self.assertIs(copied_items[0], copied)
                copied_items.append("snapshot")
                exported = snapshot.to_dict()
                exported["items"].append("export")
                self.assertEqual(items, [error])
                self.assertEqual(snapshot["items"], [copied, "snapshot"])
                self.assertIs(exported["items"][0], exported["error"])
                self.assertEqual(len(exported["items"]), 3)

    def test_reports_redact_unsafe_cached_members_and_native_exception_fields(self):
        for name in ("flag cached composite", "intflag cached composite",
                     "attribute error obj", "os error filename"):
            with self.subTest(case=name):
                case = unsafe_case(name)
                state = WorkflowState({"repair_history": (case.value,), "report": case.value})
                state.start()
                state.complete()
                report = generate_workflow_report(state)
                self.assertIs(type(report.repair_history[0]), str)
                self.assertIs(type(report.final_review_result), str)
                self.assertEqual(case.target.items, [])
                self.assertIs(state.get("report"), case.value)

    def test_immutable_enum_mixins_and_aliases_remain_supported(self):
        class Mixin:
            _labels = ("ready", "done")

        class Status(Mixin, Enum):
            READY = "ready"
            ALIAS = "ready"

        class Number(IntEnum):
            ONE = 1

        class Bits(Flag):
            FIRST = 1
            SECOND = 2

        class IntBits(IntFlag):
            FIRST = 1
            SECOND = 2

        values = (Status.READY, Status.ALIAS, Number.ONE, Bits.FIRST | Bits.SECOND,
                  IntBits.FIRST | IntBits.SECOND, ~Bits.FIRST, ~IntBits.FIRST)
        snapshot = WorkflowState({"values": values}).snapshot()
        self.assertEqual(snapshot["values"], values)
        self.assertIs(snapshot["values"][0], Status.READY)
        self.assertIs(snapshot["values"][0], snapshot["values"][1])
        self.assertEqual(snapshot["values"][0]._labels, ("ready", "done"))
        self.assertEqual(snapshot.to_dict()["values"], values)

    def test_enum_inherited_data_is_rechecked_between_snapshots(self):
        class Mixin:
            _items = ()

        class Status(Mixin, Enum):
            READY = "ready"

        state = WorkflowState({"status": Status.READY})
        self.assertIs(state.snapshot()["status"], Status.READY)
        Mixin._items = []
        with self.assertRaises(TypeError):
            state.snapshot()
        Mixin._items = ()
        self.assertIs(state.snapshot()["status"], Status.READY)

    def test_inherited_data_is_rechecked_and_instance_overrides_are_detached(self):
        class Parent:
            pass

        class Child(Parent):
            pass

        value = Child()
        state = WorkflowState({"value": value})
        state.snapshot()
        Parent._items = []
        with self.assertRaises(TypeError):
            state.snapshot()
        items = []
        value._items = items
        state = WorkflowState({"value": value, "items": items})
        snapshot = state.snapshot()
        self.assertIs(snapshot["value"]._items, snapshot["items"])
        snapshot["value"]._items.append("snapshot")
        self.assertEqual(items, [])
        self.assertEqual(Parent._items, [])

    def test_exception_tracebacks_are_rejected_before_and_after_copy(self):
        try:
            raise ValueError("original traceback")
        except ValueError as error:
            traceback = error.__traceback__
        for introduced_by_copy in (False, True):
            with self.subTest(introduced_by_copy=introduced_by_copy):
                class Error(ValueError):
                    def __deepcopy__(self, memo):
                        copied = type(self)(*deepcopy(self.args, memo))
                        copied.__traceback__ = traceback
                        return copied

                value = Error("outer")
                if not introduced_by_copy:
                    value.__traceback__ = traceback
                with self.assertRaises(TypeError):
                    WorkflowState({"error": value}).snapshot()
                state = WorkflowState({"value": "original"})
                with self.assertRaises(TypeError):
                    state.apply({"value": "new", "error": value}, source="prepare")
                self.assertEqual(state.to_dict(), {"value": "original"})
                self.assertEqual(state.version, 0)
                self.assertEqual(state.history, ())

    def test_exception_chain_can_be_detached_with_aliases_and_cycles(self):
        class Error(ValueError):
            def __deepcopy__(self, memo):
                copied = type(self).__new__(type(self))
                memo[id(self)] = copied
                copied.args = deepcopy(self.args, memo)
                copied.__cause__ = deepcopy(self.__cause__, memo)
                copied.__context__ = deepcopy(self.__context__, memo)
                return copied

        items = []
        error = Error(items)
        child = Error(items)
        error.__cause__ = child
        error.__context__ = child
        child.__cause__ = error
        snapshot = WorkflowState({"items": items, "error": error}).snapshot()
        copied = snapshot["error"]
        self.assertIs(copied.__cause__, copied.__context__)
        self.assertIs(copied.__cause__.__cause__, copied)
        self.assertIs(copied.args[0], snapshot["items"])
        self.assertIs(copied.__cause__.args[0], snapshot["items"])
        copied.__cause__.args[0].append("snapshot")
        exported = snapshot.to_dict()
        exported["error"].__context__.args[0].append("export")
        self.assertEqual(items, [])
        self.assertEqual(snapshot["items"], ["snapshot"])
        self.assertEqual(exported["items"], ["snapshot", "export"])

    def test_unsafe_values_are_rejected_in_nested_containers_and_attributes(self):
        wrappers = (
            lambda value: value,
            lambda value: {"links": deque([(value,)])},
            lambda value: MappingProxyType({"value": value}),
            lambda value: SimpleNamespace(data=value),
        )
        for name in UNSAFE_CASES:
            for index, wrap in enumerate(wrappers):
                with self.subTest(case=name, wrapper=index):
                    case = unsafe_case(name)
                    state = WorkflowState({"payload": case.target, "value": wrap(case.value)})
                    with self.assertRaises(TypeError):
                        state.snapshot()
                    self.assertEqual(case.target.items, [])

    def test_failed_agents_cannot_follow_unsafe_references(self):
        for name in UNSAFE_CASES:
            with self.subTest(case=name):
                case = unsafe_case(name)
                calls = []

                class FailingAgent(BaseAgent):
                    name = "failing"

                    def run(self, snapshot):
                        calls.append(True)
                        case.mutate(snapshot["value"])
                        raise ValueError("failed after mutation")

                state = WorkflowState({"payload": case.target, "value": case.value})
                with self.assertRaises(WorkflowExecutionError):
                    WorkflowEngine().add_step("fail", FailingAgent()).run(state)
                self.assertEqual(case.target.items, [])
                self.assertEqual(calls, [])
                self.assertEqual(state.version, 0)
                self.assertEqual(state.history, ())

    def test_unsafe_apply_is_atomic(self):
        for name in UNSAFE_CASES:
            with self.subTest(case=name):
                case = unsafe_case(name)
                state = WorkflowState({"value": "original"})
                with self.assertRaises(TypeError):
                    state.apply({"value": "new", "unsafe": case.value}, source="prepare")
                self.assertEqual(state.to_dict(), {"value": "original"})
                self.assertEqual(state.version, 0)
                self.assertEqual(state.history, ())
                self.assertEqual(state.execution_logs, ())

    def test_unsafe_agent_updates_and_outputs_are_atomic(self):
        for name in UNSAFE_CASES:
            for location in ("updates", "output"):
                with self.subTest(case=name, location=location):
                    case = unsafe_case(name)
                    result = AgentResult(
                        updates={"value": "new", "unsafe": case.value} if location == "updates" else {"value": "new"},
                        output=case.value if location == "output" else None,
                    )
                    state = WorkflowState({"value": "original"})
                    with self.assertRaises(WorkflowExecutionError):
                        WorkflowEngine().add_step("produce", ReturningAgent(result)).run(state)
                    self.assertEqual(state.to_dict(), {"value": "original"})
                    self.assertEqual(state.version, 0)
                    self.assertEqual(state.history, ())
                    self.assertEqual(state.agent_results, ())
                    events = [item.event for item in state.execution_logs]
                    self.assertNotIn("state.updated", events)
                    self.assertNotIn("agent.completed", events)

    def test_copy_protocol_cannot_introduce_unsafe_references(self):
        original = Payload()
        reference = weakref.ref(original)

        class IntroducesReference:
            def __deepcopy__(self, memo):
                return SimpleNamespace(reference=reference)

        state = WorkflowState({"value": "original"})
        with self.assertRaises(TypeError):
            state.apply({"value": "new", "unsafe": IntroducesReference()}, source="prepare")
        self.assertEqual(state.to_dict(), {"value": "original"})
        self.assertEqual(state.version, 0)

    def test_copy_protocol_cannot_return_an_opaque_shared_buffer_view(self):
        class Buffer:
            def __init__(self):
                self.data = bytearray(b"abc")

            def __deepcopy__(self, memo):
                return memoryview(self.data)

        original = Buffer()
        with self.assertRaises(TypeError):
            WorkflowState({"buffer": original}).snapshot()
        state = WorkflowState({"value": "original"})
        with self.assertRaises(TypeError):
            state.apply({"value": "new", "buffer": original}, source="prepare")
        self.assertEqual(state.to_dict(), {"value": "original"})
        self.assertEqual(original.data, bytearray(b"abc"))
        self.assertEqual(state.version, 0)

    def test_snapshot_export_revalidates_locally_inserted_references(self):
        for name in UNSAFE_CASES:
            with self.subTest(case=name):
                case = unsafe_case(name)
                state = WorkflowState({"payload": {}})
                snapshot = state.snapshot()
                snapshot["payload"]["unsafe"] = case.value
                with self.assertRaises(TypeError):
                    snapshot.to_dict()
                self.assertEqual(state.to_dict(), {"payload": {}})

    def test_runtime_metadata_and_aliases_are_detached(self):
        class Label(str):
            pass

        stage, agent, message = (Label(value) for value in ("prepare", "agent", "message"))
        for value in (stage, agent, message):
            value.tags = []
        state = WorkflowState({"agent_label": agent})
        state.register_stages({stage: agent})
        state.start()
        state.begin_step(stage, agent=agent)
        state.log("custom", message, agent=agent)
        running = state.snapshot()
        self.assertIs(running["agent_label"], running.stages[stage].agent)
        running.current_step.tags.append("snapshot")
        running.stages[stage].agent.tags.append("snapshot")
        running.execution_logs[-1].message.tags.append("snapshot")
        self.assertEqual(stage.tags, [])
        self.assertEqual(agent.tags, [])
        self.assertEqual(message.tags, [])
        state.complete_step()
        finished = state.snapshot()
        finished.stage_history[0].agent.tags.append("finished")
        self.assertEqual(state.stage_history[0].agent.tags, [])
        self.assertEqual(running.stages[stage].status, StageStatus.RUNNING)

    def test_retained_result_metadata_does_not_alias_agent_input(self):
        class Label(str):
            pass

        key = Label("value")
        key.tags = []
        state = WorkflowState()
        state.start()
        state.begin_step("produce", agent="producer")
        state.commit_agent_result(
            agent="producer", updates={key: 1}, output=None,
            updated_keys=(key,), next_step=None,
        )

        key.tags.append("uncommitted")

        record = state.agent_results[0]
        self.assertEqual(record.updated_keys[0].tags, [])
        self.assertIs(next(iter(record.updates)), record.updated_keys[0])
        self.assertEqual(state.version, 1)

    def test_supported_indirect_data_preserves_aliases_and_types(self):
        items = []
        mapping = defaultdict(list, existing=items)
        state = WorkflowState({
            "items": items, "mapping": mapping,
            "partial": partial(append_item, items, "partial"),
            "slice": slice(items, None, None), "error": ValueError(items),
        })
        snapshot = state.snapshot()
        snapshot["partial"]()
        snapshot["mapping"]["new"].append("new")
        snapshot["slice"].start.append("slice")
        snapshot["error"].args[0].append("exception")
        self.assertIs(snapshot["items"], snapshot["mapping"]["existing"])
        self.assertEqual(snapshot["items"], ["partial", "slice", "exception"])
        self.assertEqual(items, [])
        self.assertNotIn("new", mapping)

    def test_known_immutable_values_remain_supported(self):
        class Status(Enum):
            READY = "ready"
        values = (None, True, 1, 2.5, complex(1, 2), "text", b"data", range(3),
                  Decimal("1.5"), Fraction(1, 3), re.compile("x"), date(2026, 1, 1),
                  datetime(2026, 1, 1, tzinfo=timezone.utc), timedelta(seconds=1),
                  timezone.utc, Status.READY, frozenset({1, 2}), len)
        snapshot = WorkflowState({"values": values}).snapshot()
        self.assertEqual(snapshot["values"], values)
        self.assertEqual(snapshot.to_dict()["values"], values)

    def test_primitive_buffers_remain_supported_and_detached(self):
        data = bytearray(b"abc")
        numbers = array("i", [1])
        snapshot = WorkflowState({"data": data, "numbers": numbers}).snapshot()
        snapshot["data"][0] = ord("z")
        snapshot["numbers"].append(2)
        self.assertEqual(data, bytearray(b"abc"))
        self.assertEqual(numbers.tolist(), [1])

    def test_rejected_iterator_is_not_consumed(self):
        calls = []

        class Iterator:
            def __iter__(self):
                return self

            def __next__(self):
                calls.append(True)
                return 1

        with self.assertRaises(TypeError):
            WorkflowState({"value": Iterator()}).snapshot()
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
