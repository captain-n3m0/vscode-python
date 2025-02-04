import json
import os
import pathlib
import sys
import traceback

import pytest

script_dir = pathlib.Path(__file__).parent.parent
sys.path.append(os.fspath(script_dir))
sys.path.append(os.fspath(script_dir / "lib" / "python"))

from typing import Any, Dict, List, Optional, Union

from testing_tools import socket_manager
from typing_extensions import Literal, TypedDict


class TestData(TypedDict):
    """A general class that all test objects inherit from."""

    name: str
    path: str
    type_: Literal["class", "file", "folder", "test", "error"]
    id_: str


class TestItem(TestData):
    """A class defining test items."""

    lineno: str
    runID: str


class TestNode(TestData):
    """A general class that handles all test data which contains children."""

    children: "list[Union[TestNode, TestItem, None]]"


class VSCodePytestError(Exception):
    """A custom exception class for pytest errors."""

    def __init__(self, message):
        super().__init__(message)


ERRORS = []


def pytest_internalerror(excrepr, excinfo):
    """A pytest hook that is called when an internal error occurs.

    Keyword arguments:
    excrepr -- the exception representation.
    excinfo -- the exception information of type ExceptionInfo.
    """
    # call.excinfo.exconly() returns the exception as a string.
    ERRORS.append(excinfo.exconly())


def pytest_exception_interact(node, call, report):
    """A pytest hook that is called when an exception is raised which could be handled.

    Keyword arguments:
    node -- the node that raised the exception.
    call -- the call object.
    report -- the report object of either type CollectReport or TestReport.
    """
    # call.excinfo is the captured exception of the call, if it raised as type ExceptionInfo.
    # call.excinfo.exconly() returns the exception as a string.
    ERRORS.append(call.excinfo.exconly())


def pytest_keyboard_interrupt(excinfo):
    """A pytest hook that is called when a keyboard interrupt is raised.

    Keyword arguments:
    excinfo -- the exception information of type ExceptionInfo.
    """
    # The function execonly() returns the exception as a string.
    ERRORS.append(excinfo.exconly())


def pytest_sessionfinish(session, exitstatus):
    """A pytest hook that is called after pytest has fulled finished.

    Keyword arguments:
    session -- the pytest session object.
    exitstatus -- the status code of the session.
    """
    cwd = pathlib.Path.cwd()
    try:
        session_node: Union[TestNode, None] = build_test_tree(session)
        if not session_node:
            raise VSCodePytestError(
                "Something went wrong following pytest finish, \
                    no session node was created"
            )
        post_response(os.fsdecode(cwd), session_node)
    except Exception as e:
        ERRORS.append(
            f"Error Occurred, traceback: {(traceback.format_exc() if e.__traceback__ else '')}"
        )
        errorNode: TestNode = {
            "name": "",
            "path": "",
            "type_": "error",
            "children": [],
            "id_": "",
        }
        post_response(os.fsdecode(cwd), errorNode)


def build_test_tree(session: pytest.Session) -> TestNode:
    """Builds a tree made up of testing nodes from the pytest session.

    Keyword arguments:
    session -- the pytest session object.
    """
    session_node = create_session_node(session)
    session_children_dict: Dict[str, TestNode] = {}
    file_nodes_dict: Dict[Any, TestNode] = {}
    class_nodes_dict: Dict[str, TestNode] = {}

    for test_case in session.items:
        test_node = create_test_node(test_case)
        if isinstance(test_case.parent, pytest.Class):
            try:
                test_class_node = class_nodes_dict[test_case.parent.name]
            except KeyError:
                test_class_node = create_class_node(test_case.parent)
                class_nodes_dict[test_case.parent.name] = test_class_node
            test_class_node["children"].append(test_node)
            if test_case.parent.parent:
                parent_module = test_case.parent.parent
            else:
                ERRORS.append(f"Test class {test_case.parent} has no parent")
                break
            # Create a file node that has the class as a child.
            try:
                test_file_node: TestNode = file_nodes_dict[parent_module]
            except KeyError:
                test_file_node = create_file_node(parent_module)
                file_nodes_dict[parent_module] = test_file_node
            # Check if the class is already a child of the file node.
            if test_class_node not in test_file_node["children"]:
                test_file_node["children"].append(test_class_node)
        else:  # This includes test cases that are pytest functions or a doctests.
            try:
                parent_test_case = file_nodes_dict[test_case.parent]
            except KeyError:
                parent_test_case = create_file_node(test_case.parent)
                file_nodes_dict[test_case.parent] = parent_test_case
            parent_test_case["children"].append(test_node)
    created_files_folders_dict: Dict[str, TestNode] = {}
    for file_module, file_node in file_nodes_dict.items():
        # Iterate through all the files that exist and construct them into nested folders.
        root_folder_node: TestNode = build_nested_folders(
            file_module, file_node, created_files_folders_dict, session
        )
        # The final folder we get to is the highest folder in the path
        # and therefore we add this as a child to the session.
        root_id = root_folder_node.get("id_")
        if root_id and root_id not in session_children_dict:
            session_children_dict[root_id] = root_folder_node
    session_node["children"] = list(session_children_dict.values())
    return session_node


def build_nested_folders(
    file_module: Any,
    file_node: TestNode,
    created_files_folders_dict: Dict[str, TestNode],
    session: pytest.Session,
) -> TestNode:
    """Takes a file or folder and builds the nested folder structure for it.

    Keyword arguments:
    file_module -- the created module for the file we  are nesting.
    file_node -- the file node that we are building the nested folders for.
    created_files_folders_dict -- Dictionary of all the folders and files that have been created.
    session -- the pytest session object.
    """
    prev_folder_node = file_node

    # Begin the iterator_path one level above the current file.
    iterator_path = file_module.path.parent
    while iterator_path != session.path:
        curr_folder_name = iterator_path.name
        try:
            curr_folder_node: TestNode = created_files_folders_dict[curr_folder_name]
        except KeyError:
            curr_folder_node: TestNode = create_folder_node(
                curr_folder_name, iterator_path
            )
            created_files_folders_dict[curr_folder_name] = curr_folder_node
        if prev_folder_node not in curr_folder_node["children"]:
            curr_folder_node["children"].append(prev_folder_node)
        iterator_path = iterator_path.parent
        prev_folder_node = curr_folder_node
    return prev_folder_node


def create_test_node(
    test_case: pytest.Item,
) -> TestItem:
    """Creates a test node from a pytest test case.

    Keyword arguments:
    test_case -- the pytest test case.
    """
    test_case_loc: str = (
        str(test_case.location[1] + 1) if (test_case.location[1] is not None) else ""
    )
    return {
        "name": test_case.name,
        "path": os.fspath(test_case.path),
        "lineno": test_case_loc,
        "type_": "test",
        "id_": test_case.nodeid,
        "runID": test_case.nodeid,
    }


def create_session_node(session: pytest.Session) -> TestNode:
    """Creates a session node from a pytest session.

    Keyword arguments:
    session -- the pytest session.
    """
    return {
        "name": session.name,
        "path": os.fspath(session.path),
        "type_": "folder",
        "children": [],
        "id_": os.fspath(session.path),
    }


def create_class_node(class_module: pytest.Class) -> TestNode:
    """Creates a class node from a pytest class object.

    Keyword arguments:
    class_module -- the pytest object representing a class module.
    """
    return {
        "name": class_module.name,
        "path": os.fspath(class_module.path),
        "type_": "class",
        "children": [],
        "id_": class_module.nodeid,
    }


def create_file_node(file_module: Any) -> TestNode:
    """Creates a file node from a pytest file module.

    Keyword arguments:
    file_module -- the pytest file module.
    """
    return {
        "name": file_module.path.name,
        "path": os.fspath(file_module.path),
        "type_": "file",
        "id_": os.fspath(file_module.path),
        "children": [],
    }


def create_folder_node(folderName: str, path_iterator: pathlib.Path) -> TestNode:
    """Creates a folder node from a pytest folder name and its path.

    Keyword arguments:
    folderName -- the name of the folder.
    path_iterator -- the path of the folder.
    """
    return {
        "name": folderName,
        "path": os.fspath(path_iterator),
        "type_": "folder",
        "id_": os.fspath(path_iterator),
        "children": [],
    }


class PayloadDict(TypedDict):
    """A dictionary that is used to send a post request to the server."""

    cwd: str
    status: Literal["success", "error"]
    tests: Optional[TestNode]
    errors: Optional[List[str]]


def post_response(cwd: str, session_node: TestNode) -> None:
    """Sends a post request to the server.

    Keyword arguments:
    cwd -- the current working directory.
    session_node -- the session node, which is the top of the testing tree.
    errors -- a list of errors that occurred during test collection.
    """
    payload: PayloadDict = {
        "cwd": cwd,
        "status": "success" if not ERRORS else "error",
        "tests": session_node,
        "errors": [],
    }
    if ERRORS:
        payload["errors"] = ERRORS

    testPort: Union[str, int] = os.getenv("TEST_PORT", 45454)
    testuuid: Union[str, None] = os.getenv("TEST_UUID")
    addr = "localhost", int(testPort)
    data = json.dumps(payload)
    request = f"""Content-Length: {len(data)}
Content-Type: application/json
Request-uuid: {testuuid}

{data}"""
    test_output_file: Optional[str] = os.getenv("TEST_OUTPUT_FILE", None)
    if test_output_file == "stdout":
        print(request)
    elif test_output_file:
        pathlib.Path(test_output_file).write_text(request, encoding="utf-8")
    else:
        try:
            with socket_manager.SocketManager(addr) as s:
                if s.socket is not None:
                    s.socket.sendall(request.encode("utf-8"))
        except Exception as e:
            print(f"Plugin error connection error[vscode-pytest]: {e}")
            print(f"[vscode-pytest] data: {request}")
