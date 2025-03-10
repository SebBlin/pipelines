# Copyright 2021 Google LLC. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Module for creating pipeline components based on AI Platform SDK."""

import collections
import docstring_parser
import inspect
import json
from typing import Any, Callable, Dict, Optional, Tuple, Union

from google.cloud import aiplatform
import kfp
from kfp import components

# prefix for keyword arguments to separate constructor and method args
INIT_KEY = 'init'
METHOD_KEY = 'method'

# Container image that is used for component containers
# TODO tie the container version to sdk release version instead of latest
DEFAULT_CONTAINER_IMAGE = 'gcr.io/sashaproject-1/aiplatform_component:latest'

# map of MB SDK type to Metadata type
RESOURCE_TO_METADATA_TYPE = {
    aiplatform.datasets.dataset._Dataset: "Dataset",  # pylint: disable=protected-access
    aiplatform.Model: "Model",
    aiplatform.Endpoint: "Artifact",
    aiplatform.BatchPredictionJob: "Artifact"
}


def get_forward_reference(
    annotation: Any
) -> Optional[aiplatform.base.AiPlatformResourceNoun]:
    """Resolves forward references to AiPlatform Class."""

    def get_aiplatform_class_by_name(_annotation):
        """Resolves str annotation to AiPlatfrom Class."""
        if isinstance(_annotation, str):
            return getattr(aiplatform, _annotation, None)

    ai_platform_class = get_aiplatform_class_by_name(annotation)
    if ai_platform_class:
        return ai_platform_class

    try:
        # Python 3.7+
        from typing import ForwardRef
        if isinstance(annotation, ForwardRef):
            annotation = annotation.__forward_arg__
            ai_platform_class = get_aiplatform_class_by_name(annotation)
            if ai_platform_class:
                return ai_platform_class

    except ImportError:
        pass


def resolve_annotation(annotation: Any) -> Any:
    """Resolves annotation type against a MB SDK type.

    Use this for Optional, Union, Forward References

    Args:
        annotation: Annotation to resolve
    Returns:
        Direct annotation
    """

    # handle forward reference string

    # if this is an Ai Platform resource noun
    if inspect.isclass(annotation):
        if issubclass(annotation, aiplatform.base.AiPlatformResourceNoun):
            return annotation

    # handle forward references
    resolved_annotation = get_forward_reference(annotation)
    if resolved_annotation:
        return resolved_annotation

    # handle optional types
    if getattr(annotation, '__origin__', None) is Union:
        # assume optional type
        # TODO check for optional type
        resolved_annotation = get_forward_reference(annotation.__args__[0])
        if resolved_annotation:
            return resolved_annotation
        else:
            return annotation.__args__[0]

    if annotation is inspect._empty:
        return None

    return annotation


def is_serializable_to_json(annotation: Any) -> bool:
    """Checks if the type is serializable.

    Args:
        annotation: parameter annotation
    Returns:
        True if serializable to json.
    """
    serializable_types = (dict, list, collections.abc.Sequence)
    return getattr(annotation, '__origin__', None) in serializable_types


def is_mb_sdk_resource_noun_type(mb_sdk_type: Any) -> bool:
    """Determines if type passed in should be a metadata type.

    Args:
        mb_sdk_type: Type to check
    Returns:
        True if this is a resource noun
    """
    if inspect.isclass(mb_sdk_type):
        return issubclass(mb_sdk_type, aiplatform.base.AiPlatformResourceNoun)
    return False


def get_serializer(annotation: Any) -> Optional[Callable]:
    """Get a serializer for objects to pass them as strings.

    Remote runner will deserialize.
    # TODO handle proto.Message

    Args:
        annotation: Parameter annotation
    Returns:
        serializer for that annotation type
    """
    if is_serializable_to_json(annotation):
        return json.dumps


def get_deserializer(annotation: Any) -> Optional[Callable[..., str]]:
    """Get deserializer for objects to pass them as strings.

    Remote runner will deserialize.
    # TODO handle proto.Message
    Args:
        annotation: parameter annotation
    Returns:
        deserializer for annotation type
    """
    if is_serializable_to_json(annotation):
        return json.loads


def map_resource_to_metadata_type(
    mb_sdk_type: aiplatform.base.AiPlatformResourceNoun
) -> Tuple[str, str]:
    """Maps an MB SDK type to Metadata type.

    Returns:
        Tuple of component parameter name and metadata type.
        ie aiplatform.Model -> "model", "Model"
    """

    # type should always be in this map
    if is_mb_sdk_resource_noun_type(mb_sdk_type):
        for key in RESOURCE_TO_METADATA_TYPE.keys():
            if issubclass(mb_sdk_type, key):
                parameter_name = key.__name__.split('.')[-1].lower()

                # replace leading _ for example _Dataset
                if parameter_name.startswith("_"):
                    parameter_name = parameter_name[1:]

                return parameter_name, RESOURCE_TO_METADATA_TYPE[key]

    # handles the case of exported_dataset
    # TODO generalize to all serializable outputs
    if is_serializable_to_json(mb_sdk_type):
        return "exported_dataset", "JsonArray"

    # handles the case of imported datasets
    if mb_sdk_type == '_Dataset':
        return "dataset", "Dataset"


def should_be_metadata_type(mb_sdk_type: Any) -> bool:
    """Determines if type passed in should be a metadata type."""
    if inspect.isclass(mb_sdk_type):
        return issubclass(mb_sdk_type, aiplatform.base.AiPlatformResourceNoun)
    return False


def is_resource_name_parameter_name(param_name: str) -> bool:
    """Determines if the mb_sdk parameter is a resource name."""
    return param_name != 'display_name' and \
            not param_name.endswith('encryption_spec_key_name') and \
            param_name.endswith('_name')


# These parameters are filtered from MB SDK methods
PARAMS_TO_REMOVE = {"self", "credentials", "sync"}


def filter_signature(
    signature: inspect.Signature,
    is_init_signature: bool = False,
    self_type: Optional[aiplatform.base.AiPlatformResourceNoun] = None,
    component_param_name_to_mb_sdk_param_name: Dict[str, str] = None
) -> inspect.Signature:
    """Removes unused params from signature.

    Args:
        signature (inspect.Signature): Model Builder SDK Method Signature.
        is_init_signature (bool): is this constructor signature
        self_type (aiplatform.base.AiPlatformResourceNoun): This is used to replace *_name str fields with resource
            name type.
        component_param_name_to_mb_sdk_param_name dict[str, str]: Mapping to keep track of param names changed
            to make them component friendly( ie: model_name -> model)

    Returns:
        Signature appropriate for component creation.
    """
    new_params = []
    for param in signature.parameters.values():
        if param.name not in PARAMS_TO_REMOVE:
            # change resource name signatures to resource types
            # to enforce metadata entry
            # ie: model_name -> model
            if is_init_signature and is_resource_name_parameter_name(param.name
                                                                    ):
                new_name = param.name[:-len('_name')]
                new_params.append(
                    inspect.Parameter(
                        name=new_name,
                        kind=param.kind,
                        default=param.default,
                        annotation=self_type
                    )
                )
                component_param_name_to_mb_sdk_param_name[new_name] = param.name
            else:
                new_params.append(param)

    return inspect.Signature(
        parameters=new_params, return_annotation=signature.return_annotation
    )


def signatures_union(
    init_sig: inspect.Signature, method_sig: inspect.Signature
) -> inspect.Signature:
    """Returns a Union of the constructor and method signature.

    Args:
        init_sig (inspect.Signature): Constructor signature
        method_sig (inspect.Signature): Method signature

    Returns:
        A Union of the the two Signatures as a single Signature
    """

    def key(param):
        # all params are keyword or positional
        # move the params without defaults to the front
        if param.default is inspect._empty:
            return -1
        return 1

    params = list(init_sig.parameters.values()
                 ) + list(method_sig.parameters.values())
    params.sort(key=key)
    return inspect.Signature(
        parameters=params, return_annotation=method_sig.return_annotation
    )


def filter_docstring_args(
    signature: inspect.Signature,
    docstring: str,
    is_init_signature: bool = False,
) -> Dict[str, str]:
    """Removes unused params from docstring Args section.

    Args:
        signature (inspect.Signature): Model Builder SDK Method Signature.
        docstring (str): Model Builder SDK Method docstring from method.__doc__
        is_init_signature (bool): is this constructor signature

    Returns:
        Dictionary of Arg names as keys and descriptions as values.
    """
    try:
        parsed_docstring = docstring_parser.parse(docstring)
    except ValueError:
        return {}
    args_dict = {p.arg_name: p.description for p in parsed_docstring.params}

    new_args_dict = {}
    for param in signature.parameters.values():
        if param.name not in PARAMS_TO_REMOVE:
            new_arg_name = param.name
            # change resource name signatures to resource types
            # to match new param.names ie: model_name -> model
            if is_init_signature and is_resource_name_parameter_name(param.name
                                                                    ):
                new_arg_name = param.name[:-len('_name')]

            # check if there was an arg description for this parameter.
            if args_dict.get(param.name):
                new_args_dict[new_arg_name] = args_dict.get(param.name)
    return new_args_dict


def generate_docstring(
    args_dict: Dict[str, str], signature: inspect.Signature,
    method_docstring: str
) -> str:
    """Generates a new doc string using args_dict provided.

    Args:
        args_dict (Dict[str, str]): A dictionary of Arg names as keys and descriptions as values.
        signature (inspect.Signature): Method Signature of the converted method.
        method_docstring (str): Model Builder SDK Method docstring from method.__doc__
    Returns:
        A doc string for converted method.
    """
    try:
        parsed_docstring = docstring_parser.parse(method_docstring)
    except ValueError:
        # If failed to parse docstring use the origional instead
        # TODO Log Warning that parsing docstring failed.
        return method_docstring

    doc = f"{parsed_docstring.short_description}\n"
    if parsed_docstring.long_description:
        doc += f"{parsed_docstring.long_description}\n"
    if args_dict:
        doc += "Args:\n"
        for key, val in args_dict.items():
            formated_description = val.replace("\n", "\n        ")
            doc = doc + f"    {key}:\n        {formated_description}\n"

    if parsed_docstring.returns:
        formated_return = parsed_docstring.returns.description.replace(
            "\n", "\n        "
        )
        doc += "Returns:\n"
        doc += f"        {formated_return}\n"

    if parsed_docstring.raises:
        doc += "Raises:\n"
        raises_dict = {
            p.type_name: p.description for p in parsed_docstring.raises
        }
        for key, val in args_dict.items():
            formated_description = val.replace("\n", "\n        ")
            doc = doc + f"    {key}:\n        {formated_description}\n"
    return doc


def convert_method_to_component(
    cls: aiplatform.base.AiPlatformResourceNoun, method: Callable
) -> Callable:
    """Converts a MB SDK Method to a Component wrapper.

    The wrapper enforces the correct signature w.r.t the MB SDK. The signature
    is also available to inspect.

    For example:

    aiplatform.Model.deploy is converted to ModelDeployOp

    Which can be called:
        model_deploy_step = ModelDeployOp(
            project=project,  # Pipeline parameter
            endpoint=endpoint_create_step.outputs['endpoint'],
            model=model_upload_step.outputs['model'],
            deployed_model_display_name='my-deployed-model',
            machine_type='n1-standard-4',
        )

    Generates and invokes the following Component:

    name: Model-deploy
    inputs:
    - {name: project, type: String}
    - {name: endpoint, type: Artifact}
    - {name: model, type: Model}
    outputs:
    - {name: endpoint, type: Artifact}
    implementation:
      container:
        image: gcr.io/sashaproject-1/mb_sdk_component:latest
        command:
        - python3
        - remote_runner.py
        - --cls_name=Model
        - --method_name=deploy
        - --method.deployed_model_display_name=my-deployed-model
        - --method.machine_type=n1-standard-4
        args:
        - --resource_name_output_artifact_path
        - {outputPath: endpoint}
        - --init.project
        - {inputValue: project}
        - --method.endpoint
        - {inputPath: endpoint}
        - --init.model_name
        - {inputPath: model}


    Args:
        method (Callable): A MB SDK Method
        should_serialize_init (bool): Whether to also include the constructor params
            in the component
    Returns:
        A Component wrapper that accepts the MB SDK params and returns a Task.
    """
    method_name = method.__name__
    method_signature = inspect.signature(method)

    cls_name = cls.__name__
    init_method = cls.__init__
    init_signature = inspect.signature(init_method)

    should_serialize_init = inspect.isfunction(method)

    # map to store parameter names that are changed in components
    # this is generally used for constructor where the mb sdk takes
    # a resource name but the component takes a metadata entry
    # ie: model: system.Model -> model_name: str
    component_param_name_to_mb_sdk_param_name = {}
    # remove unused parameters
    method_signature = filter_signature(method_signature)
    init_signature = filter_signature(
        init_signature,
        is_init_signature=True,
        self_type=cls,
        component_param_name_to_mb_sdk_param_name=
        component_param_name_to_mb_sdk_param_name
    )

    # use this to partition args to method or constructor
    init_arg_names = set(init_signature.parameters.keys()
                        ) if should_serialize_init else set([])

    # determines outputs for this component
    output_type = resolve_annotation(method_signature.return_annotation)
    outputs = ''
    output_args = ''
    if output_type:
        output_metadata_name, output_metadata_type = map_resource_to_metadata_type(
            output_type
        )
        outputs = '\n'.join([
            'outputs:',
            f'- {{name: {output_metadata_name}, type: {output_metadata_type}}}'
        ])
        output_args = '\n'.join([
            '    - --resource_name_output_artifact_path',
            f'    - {{outputPath: {output_metadata_name}}}',
        ])

    def make_args(args_to_serialize: Dict[str, Dict[str, Any]]) -> str:
        """Takes the args dictionary and return serialized Component string for
        args.

        Args:
            args_to_serialize: Dictionary of format
                {'init': {'param_name_1': param_1}, {'method'}: {'param_name_2': param_name_2}}
        Returns:
            Serialized args compatible with Component YAML
        """
        additional_args = []
        for key, args in args_to_serialize.items():
            for arg_key, value in args.items():
                additional_args.append(f"    - --{key}.{arg_key}={value}")
        return '\n'.join(additional_args)

    def component_yaml_generator(**kwargs):
        inputs = ["inputs:"]
        input_args = []
        input_kwargs = {}

        serialized_args = {INIT_KEY: {}, METHOD_KEY: {}}

        init_kwargs = {}
        method_kwargs = {}

        for key, value in kwargs.items():
            if key in init_arg_names:
                prefix_key = INIT_KEY
                init_kwargs[key] = value
                signature = init_signature
            else:
                prefix_key = METHOD_KEY
                method_kwargs[key] = value
                signature = method_signature

            # no need to add this argument because it's optional
            # this param is validated against the signature because
            # of init_kwargs, method_kwargs
            if value is None:
                continue

            param_type = signature.parameters[key].annotation
            param_type = resolve_annotation(param_type)
            serializer = get_serializer(param_type)
            if serializer:
                param_type = str
                value = serializer(value)

            # TODO remove PipelineParam check when Metadata Importer component available
            # if we serialize we need to include the argument as input
            # perhaps, another option is to embed in yaml as json serialized list
            component_param_name = component_param_name_to_mb_sdk_param_name.get(
                key, key
            )
            if isinstance(value,
                          kfp.dsl._pipeline_param.PipelineParam) or serializer:
                if is_mb_sdk_resource_noun_type(param_type):
                    metadata_type = map_resource_to_metadata_type(param_type)[1]
                    component_param_type, component_type = metadata_type, 'inputPath'
                else:
                    component_param_type, component_type = 'String', 'inputValue'

                inputs.append(
                    f"- {{name: {key}, type: {component_param_type}}}"
                )
                input_args.append(
                    '\n'.join([
                        f'    - --{prefix_key}.{component_param_name}',
                        f'    - {{{component_type}: {key}}}'
                    ])
                )
                input_kwargs[key] = value
            else:
                serialized_args[prefix_key][component_param_name] = value

        # validate parameters
        if should_serialize_init:
            init_signature.bind(**init_kwargs)
        method_signature.bind(**method_kwargs)

        inputs = "\n".join(inputs) if len(inputs) > 1 else ''
        input_args = "\n".join(input_args) if input_args else ''
        component_text = "\n".join([
            f'name: {cls_name}-{method_name}', f'{inputs}', outputs,
            'implementation:', '  container:',
            f'    image: {DEFAULT_CONTAINER_IMAGE}', '    command:',
            '    - python3', '    - -m',
            '    - google_cloud_components.aiplatform.remote_runner',
            f'    - --cls_name={cls_name}',
            f'    - --method_name={method_name}',
            f'{make_args(serialized_args)}', '    args:', output_args,
            f'{input_args}'
        ])

        print(component_text)

        return components.load_component_from_text(component_text)(
            **input_kwargs
        )

    component_yaml_generator.__signature__ = signatures_union(
        init_signature, method_signature
    ) if should_serialize_init else method_signature

    # Create a docstring based on the new signature.
    new_args_dict = {}
    new_args_dict.update(
        filter_docstring_args(
            signature=method_signature,
            docstring=inspect.getdoc(method),
            is_init_signature=False
        )
    )
    if should_serialize_init:
        new_args_dict.update(
            filter_docstring_args(
                signature=init_signature,
                docstring=inspect.getdoc(init_method),
                is_init_signature=True
            )
        )
    component_yaml_generator.__doc__ = generate_docstring(
        args_dict=new_args_dict,
        signature=component_yaml_generator.__signature__,
        method_docstring=inspect.getdoc(method)
    )

    # TODO Possibly rename method

    return component_yaml_generator
