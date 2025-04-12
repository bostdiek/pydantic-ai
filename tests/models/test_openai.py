from __future__ import annotations as _annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import cached_property
from typing import Annotated, Any, Callable, Literal, Union, cast

import httpx
import pytest
from inline_snapshot import snapshot
from pydantic import BaseModel, Discriminator, Field, Tag
from typing_extensions import TypedDict

from pydantic_ai import Agent, ModelHTTPError, ModelRetry, UnexpectedModelBehavior
from pydantic_ai.messages import (
    BinaryContent,
    ImageUrl,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.gemini import GeminiModel
from pydantic_ai.providers.google_gla import GoogleGLAProvider
from pydantic_ai.result import Usage
from pydantic_ai.settings import ModelSettings

from ..conftest import IsNow, IsStr, raise_if_exception, try_import
from .mock_async_stream import MockAsyncStream

with try_import() as imports_successful:
    from openai import NOT_GIVEN, APIStatusError, AsyncOpenAI
    from openai.types import chat
    from openai.types.chat.chat_completion import Choice
    from openai.types.chat.chat_completion_chunk import (
        Choice as ChunkChoice,
        ChoiceDelta,
        ChoiceDeltaToolCall,
        ChoiceDeltaToolCallFunction,
    )
    from openai.types.chat.chat_completion_message import ChatCompletionMessage
    from openai.types.chat.chat_completion_message_tool_call import Function
    from openai.types.completion_usage import CompletionUsage, PromptTokensDetails

    from pydantic_ai.models.openai import (
        OpenAIModel,
        OpenAIModelSettings,
        OpenAIResponsesModel,
        OpenAISystemPromptRole,
        _StrictSchemaHelper,  # pyright: ignore[reportPrivateUsage]
    )
    from pydantic_ai.providers.openai import OpenAIProvider

    # note: we use Union here so that casting works with Python 3.9
    MockChatCompletion = Union[chat.ChatCompletion, Exception]
    MockChatCompletionChunk = Union[chat.ChatCompletionChunk, Exception]

pytestmark = [
    pytest.mark.skipif(not imports_successful(), reason='openai not installed'),
    pytest.mark.anyio,
]


def test_init():
    m = OpenAIModel('gpt-4o', provider=OpenAIProvider(api_key='foobar'))
    assert m.base_url == 'https://api.openai.com/v1/'
    assert m.client.api_key == 'foobar'
    assert m.model_name == 'gpt-4o'


@dataclass
class MockOpenAI:
    completions: MockChatCompletion | Sequence[MockChatCompletion] | None = None
    stream: Sequence[MockChatCompletionChunk] | Sequence[Sequence[MockChatCompletionChunk]] | None = None
    index: int = 0
    chat_completion_kwargs: list[dict[str, Any]] = field(default_factory=list)

    @cached_property
    def chat(self) -> Any:
        chat_completions = type('Completions', (), {'create': self.chat_completions_create})
        return type('Chat', (), {'completions': chat_completions})

    @classmethod
    def create_mock(cls, completions: MockChatCompletion | Sequence[MockChatCompletion]) -> AsyncOpenAI:
        return cast(AsyncOpenAI, cls(completions=completions))

    @classmethod
    def create_mock_stream(
        cls,
        stream: Sequence[MockChatCompletionChunk] | Sequence[Sequence[MockChatCompletionChunk]],
    ) -> AsyncOpenAI:
        return cast(AsyncOpenAI, cls(stream=stream))

    async def chat_completions_create(  # pragma: no cover
        self, *_args: Any, stream: bool = False, **kwargs: Any
    ) -> chat.ChatCompletion | MockAsyncStream[MockChatCompletionChunk]:
        self.chat_completion_kwargs.append({k: v for k, v in kwargs.items() if v is not NOT_GIVEN})

        if stream:
            assert self.stream is not None, 'you can only used `stream=True` if `stream` is provided'
            if isinstance(self.stream[0], Sequence):
                response = MockAsyncStream(iter(cast(list[MockChatCompletionChunk], self.stream[self.index])))
            else:
                response = MockAsyncStream(iter(cast(list[MockChatCompletionChunk], self.stream)))
        else:
            assert self.completions is not None, 'you can only used `stream=False` if `completions` are provided'
            if isinstance(self.completions, Sequence):
                raise_if_exception(self.completions[self.index])
                response = cast(chat.ChatCompletion, self.completions[self.index])
            else:
                raise_if_exception(self.completions)
                response = cast(chat.ChatCompletion, self.completions)
        self.index += 1
        return response


def get_mock_chat_completion_kwargs(async_open_ai: AsyncOpenAI) -> list[dict[str, Any]]:
    if isinstance(async_open_ai, MockOpenAI):
        return async_open_ai.chat_completion_kwargs
    else:  # pragma: no cover
        raise RuntimeError('Not a MockOpenAI instance')


def completion_message(message: ChatCompletionMessage, *, usage: CompletionUsage | None = None) -> chat.ChatCompletion:
    return chat.ChatCompletion(
        id='123',
        choices=[Choice(finish_reason='stop', index=0, message=message)],
        created=1704067200,  # 2024-01-01
        model='gpt-4o-123',
        object='chat.completion',
        usage=usage,
    )


async def test_request_simple_success(allow_model_requests: None):
    c = completion_message(ChatCompletionMessage(content='world', role='assistant'))
    mock_client = MockOpenAI.create_mock(c)
    m = OpenAIModel('gpt-4o', provider=OpenAIProvider(openai_client=mock_client))
    agent = Agent(m)

    result = await agent.run('hello')
    assert result.output == 'world'
    assert result.usage() == snapshot(Usage(requests=1))

    # reset the index so we get the same response again
    mock_client.index = 0  # type: ignore

    result = await agent.run('hello', message_history=result.new_messages())
    assert result.output == 'world'
    assert result.usage() == snapshot(Usage(requests=1))
    assert result.all_messages() == snapshot(
        [
            ModelRequest(parts=[UserPromptPart(content='hello', timestamp=IsNow(tz=timezone.utc))]),
            ModelResponse(
                parts=[TextPart(content='world')],
                model_name='gpt-4o-123',
                timestamp=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
            ),
            ModelRequest(parts=[UserPromptPart(content='hello', timestamp=IsNow(tz=timezone.utc))]),
            ModelResponse(
                parts=[TextPart(content='world')],
                model_name='gpt-4o-123',
                timestamp=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
            ),
        ]
    )
    assert get_mock_chat_completion_kwargs(mock_client) == [
        {
            'messages': [{'content': 'hello', 'role': 'user'}],
            'model': 'gpt-4o',
            'n': 1,
            'extra_headers': {'User-Agent': IsStr(regex=r'pydantic-ai\/.*')},
        },
        {
            'messages': [
                {'content': 'hello', 'role': 'user'},
                {'content': 'world', 'role': 'assistant'},
                {'content': 'hello', 'role': 'user'},
            ],
            'model': 'gpt-4o',
            'n': 1,
            'extra_headers': {'User-Agent': IsStr(regex=r'pydantic-ai\/.*')},
        },
    ]


async def test_request_simple_usage(allow_model_requests: None):
    c = completion_message(
        ChatCompletionMessage(content='world', role='assistant'),
        usage=CompletionUsage(completion_tokens=1, prompt_tokens=2, total_tokens=3),
    )
    mock_client = MockOpenAI.create_mock(c)
    m = OpenAIModel('gpt-4o', provider=OpenAIProvider(openai_client=mock_client))
    agent = Agent(m)

    result = await agent.run('Hello')
    assert result.output == 'world'
    assert result.usage() == snapshot(Usage(requests=1, request_tokens=2, response_tokens=1, total_tokens=3))


async def test_request_structured_response(allow_model_requests: None):
    c = completion_message(
        ChatCompletionMessage(
            content=None,
            role='assistant',
            tool_calls=[
                chat.ChatCompletionMessageToolCall(
                    id='123',
                    function=Function(arguments='{"response": [1, 2, 123]}', name='final_result'),
                    type='function',
                )
            ],
        )
    )
    mock_client = MockOpenAI.create_mock(c)
    m = OpenAIModel('gpt-4o', provider=OpenAIProvider(openai_client=mock_client))
    agent = Agent(m, output_type=list[int])

    result = await agent.run('Hello')
    assert result.output == [1, 2, 123]
    assert result.all_messages() == snapshot(
        [
            ModelRequest(parts=[UserPromptPart(content='Hello', timestamp=IsNow(tz=timezone.utc))]),
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name='final_result',
                        args='{"response": [1, 2, 123]}',
                        tool_call_id='123',
                    )
                ],
                model_name='gpt-4o-123',
                timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name='final_result',
                        content='Final result processed.',
                        tool_call_id='123',
                        timestamp=IsNow(tz=timezone.utc),
                    )
                ]
            ),
        ]
    )


async def test_request_tool_call(allow_model_requests: None):
    responses = [
        completion_message(
            ChatCompletionMessage(
                content=None,
                role='assistant',
                tool_calls=[
                    chat.ChatCompletionMessageToolCall(
                        id='1',
                        function=Function(arguments='{"loc_name": "San Fransisco"}', name='get_location'),
                        type='function',
                    )
                ],
            ),
            usage=CompletionUsage(
                completion_tokens=1,
                prompt_tokens=2,
                total_tokens=3,
                prompt_tokens_details=PromptTokensDetails(cached_tokens=1),
            ),
        ),
        completion_message(
            ChatCompletionMessage(
                content=None,
                role='assistant',
                tool_calls=[
                    chat.ChatCompletionMessageToolCall(
                        id='2',
                        function=Function(arguments='{"loc_name": "London"}', name='get_location'),
                        type='function',
                    )
                ],
            ),
            usage=CompletionUsage(
                completion_tokens=2,
                prompt_tokens=3,
                total_tokens=6,
                prompt_tokens_details=PromptTokensDetails(cached_tokens=2),
            ),
        ),
        completion_message(ChatCompletionMessage(content='final response', role='assistant')),
    ]
    mock_client = MockOpenAI.create_mock(responses)
    m = OpenAIModel('gpt-4o', provider=OpenAIProvider(openai_client=mock_client))
    agent = Agent(m, system_prompt='this is the system prompt')

    @agent.tool_plain
    async def get_location(loc_name: str) -> str:
        if loc_name == 'London':
            return json.dumps({'lat': 51, 'lng': 0})
        else:
            raise ModelRetry('Wrong location, please try again')

    result = await agent.run('Hello')
    assert result.output == 'final response'
    assert result.all_messages() == snapshot(
        [
            ModelRequest(
                parts=[
                    SystemPromptPart(content='this is the system prompt', timestamp=IsNow(tz=timezone.utc)),
                    UserPromptPart(content='Hello', timestamp=IsNow(tz=timezone.utc)),
                ]
            ),
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name='get_location',
                        args='{"loc_name": "San Fransisco"}',
                        tool_call_id='1',
                    )
                ],
                model_name='gpt-4o-123',
                timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
            ),
            ModelRequest(
                parts=[
                    RetryPromptPart(
                        content='Wrong location, please try again',
                        tool_name='get_location',
                        tool_call_id='1',
                        timestamp=IsNow(tz=timezone.utc),
                    )
                ]
            ),
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name='get_location',
                        args='{"loc_name": "London"}',
                        tool_call_id='2',
                    )
                ],
                model_name='gpt-4o-123',
                timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name='get_location',
                        content='{"lat": 51, "lng": 0}',
                        tool_call_id='2',
                        timestamp=IsNow(tz=timezone.utc),
                    )
                ]
            ),
            ModelResponse(
                parts=[TextPart(content='final response')],
                model_name='gpt-4o-123',
                timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
            ),
        ]
    )
    assert result.usage() == snapshot(
        Usage(
            requests=3,
            request_tokens=5,
            response_tokens=3,
            total_tokens=9,
            details={'cached_tokens': 3},
        )
    )


FinishReason = Literal['stop', 'length', 'tool_calls', 'content_filter', 'function_call']


def chunk(delta: list[ChoiceDelta], finish_reason: FinishReason | None = None) -> chat.ChatCompletionChunk:
    return chat.ChatCompletionChunk(
        id='x',
        choices=[
            ChunkChoice(index=index, delta=delta, finish_reason=finish_reason) for index, delta in enumerate(delta)
        ],
        created=1704067200,  # 2024-01-01
        model='gpt-4o',
        object='chat.completion.chunk',
        usage=CompletionUsage(completion_tokens=1, prompt_tokens=2, total_tokens=3),
    )


def text_chunk(text: str, finish_reason: FinishReason | None = None) -> chat.ChatCompletionChunk:
    return chunk([ChoiceDelta(content=text, role='assistant')], finish_reason=finish_reason)


async def test_stream_text(allow_model_requests: None):
    stream = [text_chunk('hello '), text_chunk('world'), chunk([])]
    mock_client = MockOpenAI.create_mock_stream(stream)
    m = OpenAIModel('gpt-4o', provider=OpenAIProvider(openai_client=mock_client))
    agent = Agent(m)

    async with agent.run_stream('') as result:
        assert not result.is_complete
        assert [c async for c in result.stream_text(debounce_by=None)] == snapshot(['hello ', 'hello world'])
        assert result.is_complete
        assert result.usage() == snapshot(Usage(requests=1, request_tokens=6, response_tokens=3, total_tokens=9))


async def test_stream_text_finish_reason(allow_model_requests: None):
    stream = [
        text_chunk('hello '),
        text_chunk('world'),
        text_chunk('.', finish_reason='stop'),
    ]
    mock_client = MockOpenAI.create_mock_stream(stream)
    m = OpenAIModel('gpt-4o', provider=OpenAIProvider(openai_client=mock_client))
    agent = Agent(m)

    async with agent.run_stream('') as result:
        assert not result.is_complete
        assert [c async for c in result.stream_text(debounce_by=None)] == snapshot(
            ['hello ', 'hello world', 'hello world.']
        )
        assert result.is_complete


def struc_chunk(
    tool_name: str | None, tool_arguments: str | None, finish_reason: FinishReason | None = None
) -> chat.ChatCompletionChunk:
    return chunk(
        [
            ChoiceDelta(
                tool_calls=[
                    ChoiceDeltaToolCall(
                        index=0, function=ChoiceDeltaToolCallFunction(name=tool_name, arguments=tool_arguments)
                    )
                ]
            ),
        ],
        finish_reason=finish_reason,
    )


class MyTypedDict(TypedDict, total=False):
    first: str
    second: str


async def test_stream_structured(allow_model_requests: None):
    stream = [
        chunk([ChoiceDelta()]),
        chunk([ChoiceDelta(tool_calls=[])]),
        chunk([ChoiceDelta(tool_calls=[ChoiceDeltaToolCall(index=0, function=None)])]),
        chunk([ChoiceDelta(tool_calls=[ChoiceDeltaToolCall(index=0, function=None)])]),
        struc_chunk('final_result', None),
        chunk([ChoiceDelta(tool_calls=[ChoiceDeltaToolCall(index=0, function=None)])]),
        struc_chunk(None, '{"first": "One'),
        struc_chunk(None, '", "second": "Two"'),
        struc_chunk(None, '}'),
        chunk([]),
    ]
    mock_client = MockOpenAI.create_mock_stream(stream)
    m = OpenAIModel('gpt-4o', provider=OpenAIProvider(openai_client=mock_client))
    agent = Agent(m, output_type=MyTypedDict)

    async with agent.run_stream('') as result:
        assert not result.is_complete
        assert [dict(c) async for c in result.stream(debounce_by=None)] == snapshot(
            [
                {'first': 'One'},
                {'first': 'One', 'second': 'Two'},
                {'first': 'One', 'second': 'Two'},
                {'first': 'One', 'second': 'Two'},
            ]
        )
        assert result.is_complete
        assert result.usage() == snapshot(Usage(requests=1, request_tokens=20, response_tokens=10, total_tokens=30))
        # double check usage matches stream count
        assert result.usage().response_tokens == len(stream)


async def test_stream_structured_finish_reason(allow_model_requests: None):
    stream = [
        struc_chunk('final_result', None),
        struc_chunk(None, '{"first": "One'),
        struc_chunk(None, '", "second": "Two"'),
        struc_chunk(None, '}'),
        struc_chunk(None, None, finish_reason='stop'),
    ]
    mock_client = MockOpenAI.create_mock_stream(stream)
    m = OpenAIModel('gpt-4o', provider=OpenAIProvider(openai_client=mock_client))
    agent = Agent(m, output_type=MyTypedDict)

    async with agent.run_stream('') as result:
        assert not result.is_complete
        assert [dict(c) async for c in result.stream(debounce_by=None)] == snapshot(
            [
                {'first': 'One'},
                {'first': 'One', 'second': 'Two'},
                {'first': 'One', 'second': 'Two'},
                {'first': 'One', 'second': 'Two'},
                {'first': 'One', 'second': 'Two'},
            ]
        )
        assert result.is_complete


async def test_no_content(allow_model_requests: None):
    stream = [chunk([ChoiceDelta()]), chunk([ChoiceDelta()])]
    mock_client = MockOpenAI.create_mock_stream(stream)
    m = OpenAIModel('gpt-4o', provider=OpenAIProvider(openai_client=mock_client))
    agent = Agent(m, output_type=MyTypedDict)

    with pytest.raises(UnexpectedModelBehavior, match='Received empty model response'):
        async with agent.run_stream(''):
            pass  # pragma: no cover


async def test_no_delta(allow_model_requests: None):
    stream = [
        chunk([]),
        text_chunk('hello '),
        text_chunk('world'),
    ]
    mock_client = MockOpenAI.create_mock_stream(stream)
    m = OpenAIModel('gpt-4o', provider=OpenAIProvider(openai_client=mock_client))
    agent = Agent(m)

    async with agent.run_stream('') as result:
        assert not result.is_complete
        assert [c async for c in result.stream_text(debounce_by=None)] == snapshot(['hello ', 'hello world'])
        assert result.is_complete
        assert result.usage() == snapshot(Usage(requests=1, request_tokens=6, response_tokens=3, total_tokens=9))


@pytest.mark.parametrize('system_prompt_role', ['system', 'developer', 'user', None])
async def test_system_prompt_role(
    allow_model_requests: None, system_prompt_role: OpenAISystemPromptRole | None
) -> None:
    """Testing the system prompt role for OpenAI models is properly set / inferred."""

    c = completion_message(ChatCompletionMessage(content='world', role='assistant'))
    mock_client = MockOpenAI.create_mock(c)
    m = OpenAIModel('gpt-4o', system_prompt_role=system_prompt_role, provider=OpenAIProvider(openai_client=mock_client))
    assert m.system_prompt_role == system_prompt_role

    agent = Agent(m, system_prompt='some instructions')
    result = await agent.run('hello')
    assert result.output == 'world'

    assert get_mock_chat_completion_kwargs(mock_client) == [
        {
            'messages': [
                {'content': 'some instructions', 'role': system_prompt_role or 'system'},
                {'content': 'hello', 'role': 'user'},
            ],
            'model': 'gpt-4o',
            'n': 1,
            'extra_headers': {'User-Agent': IsStr(regex=r'pydantic-ai\/.*')},
        }
    ]


@pytest.mark.parametrize('system_prompt_role', ['system', 'developer'])
@pytest.mark.vcr
async def test_openai_o1_mini_system_role(
    allow_model_requests: None,
    system_prompt_role: Literal['system', 'developer'],
    openai_api_key: str,
) -> None:
    model = OpenAIModel(
        'o1-mini', provider=OpenAIProvider(api_key=openai_api_key), system_prompt_role=system_prompt_role
    )
    agent = Agent(model=model, system_prompt='You are a helpful assistant.')

    with pytest.raises(ModelHTTPError, match=r".*Unsupported value: 'messages\[0\]\.role' does not support.*"):
        await agent.run('Hello')


@pytest.mark.parametrize('parallel_tool_calls', [True, False])
async def test_parallel_tool_calls(allow_model_requests: None, parallel_tool_calls: bool) -> None:
    c = completion_message(
        ChatCompletionMessage(
            content=None,
            role='assistant',
            tool_calls=[
                chat.ChatCompletionMessageToolCall(
                    id='123',
                    function=Function(arguments='{"response": [1, 2, 3]}', name='final_result'),
                    type='function',
                )
            ],
        )
    )
    mock_client = MockOpenAI.create_mock(c)
    m = OpenAIModel('gpt-4o', provider=OpenAIProvider(openai_client=mock_client))
    agent = Agent(m, output_type=list[int], model_settings=ModelSettings(parallel_tool_calls=parallel_tool_calls))

    await agent.run('Hello')
    assert get_mock_chat_completion_kwargs(mock_client)[0]['parallel_tool_calls'] == parallel_tool_calls


async def test_image_url_input(allow_model_requests: None):
    c = completion_message(ChatCompletionMessage(content='world', role='assistant'))
    mock_client = MockOpenAI.create_mock(c)
    m = OpenAIModel('gpt-4o', provider=OpenAIProvider(openai_client=mock_client))
    agent = Agent(m)

    result = await agent.run(
        [
            'hello',
            ImageUrl(url='https://t3.ftcdn.net/jpg/00/85/79/92/360_F_85799278_0BBGV9OAdQDTLnKwAPBCcg1J7QtiieJY.jpg'),
        ]
    )
    assert result.output == 'world'
    assert get_mock_chat_completion_kwargs(mock_client) == snapshot(
        [
            {
                'model': 'gpt-4o',
                'messages': [
                    {
                        'role': 'user',
                        'content': [
                            {'text': 'hello', 'type': 'text'},
                            {
                                'image_url': {
                                    'url': 'https://t3.ftcdn.net/jpg/00/85/79/92/360_F_85799278_0BBGV9OAdQDTLnKwAPBCcg1J7QtiieJY.jpg'
                                },
                                'type': 'image_url',
                            },
                        ],
                    }
                ],
                'n': 1,
                'extra_headers': {'User-Agent': IsStr(regex=r'pydantic-ai\/.*')},
            }
        ]
    )


@pytest.mark.vcr()
async def test_image_as_binary_content_input(
    allow_model_requests: None, image_content: BinaryContent, openai_api_key: str
):
    m = OpenAIModel('gpt-4o', provider=OpenAIProvider(api_key=openai_api_key))
    agent = Agent(m)

    result = await agent.run(['What fruit is in the image?', image_content])
    assert result.output == snapshot('The fruit in the image is a kiwi.')


@pytest.mark.vcr()
async def test_audio_as_binary_content_input(
    allow_model_requests: None, audio_content: BinaryContent, openai_api_key: str
):
    m = OpenAIModel('gpt-4o-audio-preview', provider=OpenAIProvider(api_key=openai_api_key))
    agent = Agent(m)

    result = await agent.run(['Whose name is mentioned in the audio?', audio_content])
    assert result.output == snapshot('The name mentioned in the audio is Marcelo.')


def test_model_status_error(allow_model_requests: None) -> None:
    mock_client = MockOpenAI.create_mock(
        APIStatusError(
            'test error',
            response=httpx.Response(status_code=500, request=httpx.Request('POST', 'https://example.com/v1')),
            body={'error': 'test error'},
        )
    )
    m = OpenAIModel('gpt-4o', provider=OpenAIProvider(openai_client=mock_client))
    agent = Agent(m)
    with pytest.raises(ModelHTTPError) as exc_info:
        agent.run_sync('hello')
    assert str(exc_info.value) == snapshot("status_code: 500, model_name: gpt-4o, body: {'error': 'test error'}")


@pytest.mark.vcr()
@pytest.mark.parametrize('model_name', ['o3-mini', 'gpt-4o-mini', 'gpt-4.5-preview'])
async def test_max_completion_tokens(allow_model_requests: None, model_name: str, openai_api_key: str):
    m = OpenAIModel(model_name, provider=OpenAIProvider(api_key=openai_api_key))
    agent = Agent(m, model_settings=ModelSettings(max_tokens=100))

    result = await agent.run('hello')
    assert result.output == IsStr()


@pytest.mark.vcr()
async def test_multiple_agent_tool_calls(allow_model_requests: None, gemini_api_key: str, openai_api_key: str):
    gemini_model = GeminiModel('gemini-2.0-flash-exp', provider=GoogleGLAProvider(api_key=gemini_api_key))
    openai_model = OpenAIModel('gpt-4o-mini', provider=OpenAIProvider(api_key=openai_api_key))

    agent = Agent(model=gemini_model)

    @agent.tool_plain
    async def get_capital(country: str) -> str:
        """Get the capital of a country.

        Args:
            country: The country name.
        """
        if country == 'France':
            return 'Paris'
        elif country == 'England':
            return 'London'
        else:
            raise ValueError(f'Country {country} not supported.')  # pragma: no cover

    result = await agent.run('What is the capital of France?')
    assert result.output == snapshot('The capital of France is Paris.\n')

    result = await agent.run(
        'What is the capital of England?', model=openai_model, message_history=result.all_messages()
    )
    assert result.output == snapshot('The capital of England is London.')


@pytest.mark.vcr()
async def test_user_id(allow_model_requests: None, openai_api_key: str):
    # This test doesn't do anything, it's just here to ensure that calls with `user` don't cause errors, including type.
    # Since we use VCR, creating tests with an `httpx.Transport` is not possible.
    m = OpenAIModel('gpt-4o', provider=OpenAIProvider(api_key=openai_api_key))
    agent = Agent(m, model_settings=OpenAIModelSettings(openai_user='user_id'))
    await agent.run('hello')


@dataclass
class MyDefaultDc:
    x: int = 1


@dataclass
class MyRecursiveDc:
    field: MyRecursiveDc | None


@dataclass
class MyDefaultRecursiveDc:
    field: MyDefaultRecursiveDc | None = None


class MyModel(BaseModel, extra='allow'):
    pass


def strict_compatible_tool(x: int) -> str:
    return str(x)  # pragma: no cover


def tool_with_default(x: int = 1) -> str:
    return f'{x}'  # pragma: no cover


def tool_with_recursion(x: MyRecursiveDc, y: MyDefaultRecursiveDc):
    return f'{x} {y}'  # pragma: no cover


def tool_with_additional_properties(x: MyModel) -> str:
    return f'{x}'  # pragma: no cover


def tool_with_kwargs(x: int, **kwargs: Any) -> str:
    return f'{x} {kwargs}'  # pragma: no cover


def tool_with_union(x: int | MyDefaultDc) -> str:
    return f'{x}'  # pragma: no cover


def tool_with_discriminated_union(
    x: Annotated[
        Annotated[int, Tag('int')] | Annotated[MyDefaultDc, Tag('MyDefaultDc')],
        Discriminator(lambda x: type(x).__name__),
    ],
) -> str:
    return f'{x}'  # pragma: no cover


def tool_with_lists(x: list[int], y: list[MyDefaultDc]) -> str:
    return f'{x} {y}'  # pragma: no cover


def tool_with_tuples(x: tuple[int], y: tuple[str] = ('abc',)) -> str:
    return f'{x} {y}'  # pragma: no cover


@pytest.mark.parametrize(
    'tool,tool_strict,expected_params,expected_strict',
    [
        (
            strict_compatible_tool,
            False,
            snapshot(
                {
                    'additionalProperties': False,
                    'properties': {'x': {'type': 'integer'}},
                    'required': ['x'],
                    'type': 'object',
                }
            ),
            snapshot(None),
        ),
        (
            strict_compatible_tool,
            None,
            snapshot(
                {
                    'additionalProperties': False,
                    'properties': {'x': {'type': 'integer'}},
                    'required': ['x'],
                    'type': 'object',
                }
            ),
            snapshot(True),
        ),
        (
            tool_with_recursion,
            None,
            snapshot(
                {
                    '$defs': {
                        'MyDefaultRecursiveDc': {
                            'properties': {
                                'field': {
                                    'anyOf': [{'$ref': '#/$defs/MyDefaultRecursiveDc'}, {'type': 'null'}],
                                    'default': None,
                                }
                            },
                            'title': 'MyDefaultRecursiveDc',
                            'type': 'object',
                        },
                        'MyRecursiveDc': {
                            'properties': {'field': {'anyOf': [{'$ref': '#/$defs/MyRecursiveDc'}, {'type': 'null'}]}},
                            'required': ['field'],
                            'title': 'MyRecursiveDc',
                            'type': 'object',
                        },
                    },
                    'additionalProperties': False,
                    'properties': {
                        'x': {'$ref': '#/$defs/MyRecursiveDc'},
                        'y': {'$ref': '#/$defs/MyDefaultRecursiveDc'},
                    },
                    'required': ['x', 'y'],
                    'type': 'object',
                }
            ),
            snapshot(None),
        ),
        (
            tool_with_recursion,
            True,
            snapshot(
                {
                    '$defs': {
                        'MyDefaultRecursiveDc': {
                            'properties': {
                                'field': {
                                    'anyOf': [{'$ref': '#/$defs/MyDefaultRecursiveDc'}, {'type': 'null'}],
                                    'default': None,
                                }
                            },
                            'title': 'MyDefaultRecursiveDc',
                            'type': 'object',
                            'additionalProperties': False,
                            'required': ['field'],
                        },
                        'MyRecursiveDc': {
                            'properties': {'field': {'anyOf': [{'$ref': '#/$defs/MyRecursiveDc'}, {'type': 'null'}]}},
                            'title': 'MyRecursiveDc',
                            'type': 'object',
                            'additionalProperties': False,
                            'required': ['field'],
                        },
                    },
                    'additionalProperties': False,
                    'properties': {
                        'x': {'$ref': '#/$defs/MyRecursiveDc'},
                        'y': {'$ref': '#/$defs/MyDefaultRecursiveDc'},
                    },
                    'required': ['x', 'y'],
                    'type': 'object',
                }
            ),
            snapshot(True),
        ),
        (
            tool_with_additional_properties,
            None,
            snapshot(
                {
                    'additionalProperties': True,
                    'properties': {},
                    'title': 'MyModel',
                    'type': 'object',
                }
            ),
            snapshot(None),
        ),
        (
            tool_with_additional_properties,
            True,
            snapshot(
                {
                    'additionalProperties': False,
                    'properties': {},
                    'title': 'MyModel',
                    'required': [],
                    'type': 'object',
                }
            ),
            snapshot(True),
        ),
        (
            tool_with_kwargs,
            None,
            snapshot(
                {
                    'properties': {'x': {'type': 'integer'}},
                    'required': ['x'],
                    'type': 'object',
                }
            ),
            snapshot(None),
        ),
        (
            tool_with_kwargs,
            True,
            snapshot(
                {
                    'additionalProperties': False,
                    'properties': {'x': {'type': 'integer'}},
                    'required': ['x'],
                    'type': 'object',
                }
            ),
            snapshot(True),
        ),
        (
            tool_with_union,
            None,
            snapshot(
                {
                    '$defs': {
                        'MyDefaultDc': {
                            'properties': {'x': {'default': 1, 'type': 'integer'}},
                            'title': 'MyDefaultDc',
                            'type': 'object',
                        }
                    },
                    'additionalProperties': False,
                    'properties': {'x': {'anyOf': [{'type': 'integer'}, {'$ref': '#/$defs/MyDefaultDc'}]}},
                    'required': ['x'],
                    'type': 'object',
                }
            ),
            snapshot(None),
        ),
        (
            tool_with_union,
            True,
            snapshot(
                {
                    '$defs': {
                        'MyDefaultDc': {
                            'properties': {'x': {'default': 1, 'type': 'integer'}},
                            'required': ['x'],
                            'title': 'MyDefaultDc',
                            'type': 'object',
                            'additionalProperties': False,
                        }
                    },
                    'additionalProperties': False,
                    'properties': {'x': {'anyOf': [{'type': 'integer'}, {'$ref': '#/$defs/MyDefaultDc'}]}},
                    'required': ['x'],
                    'type': 'object',
                }
            ),
            snapshot(True),
        ),
        (
            tool_with_discriminated_union,
            None,
            snapshot(
                {
                    '$defs': {
                        'MyDefaultDc': {
                            'properties': {'x': {'default': 1, 'type': 'integer'}},
                            'title': 'MyDefaultDc',
                            'type': 'object',
                        }
                    },
                    'additionalProperties': False,
                    'properties': {'x': {'oneOf': [{'type': 'integer'}, {'$ref': '#/$defs/MyDefaultDc'}]}},
                    'required': ['x'],
                    'type': 'object',
                }
            ),
            snapshot(None),
        ),
        (
            tool_with_discriminated_union,
            True,
            snapshot(
                {
                    '$defs': {
                        'MyDefaultDc': {
                            'properties': {'x': {'default': 1, 'type': 'integer'}},
                            'required': ['x'],
                            'title': 'MyDefaultDc',
                            'type': 'object',
                            'additionalProperties': False,
                        }
                    },
                    'additionalProperties': False,
                    'properties': {'x': {'oneOf': [{'type': 'integer'}, {'$ref': '#/$defs/MyDefaultDc'}]}},
                    'required': ['x'],
                    'type': 'object',
                }
            ),
            snapshot(True),
        ),
        (
            tool_with_lists,
            None,
            snapshot(
                {
                    '$defs': {
                        'MyDefaultDc': {
                            'properties': {'x': {'default': 1, 'type': 'integer'}},
                            'title': 'MyDefaultDc',
                            'type': 'object',
                        }
                    },
                    'additionalProperties': False,
                    'properties': {
                        'x': {'items': {'type': 'integer'}, 'type': 'array'},
                        'y': {'items': {'$ref': '#/$defs/MyDefaultDc'}, 'type': 'array'},
                    },
                    'required': ['x', 'y'],
                    'type': 'object',
                }
            ),
            snapshot(None),
        ),
        (
            tool_with_lists,
            True,
            snapshot(
                {
                    '$defs': {
                        'MyDefaultDc': {
                            'properties': {'x': {'default': 1, 'type': 'integer'}},
                            'required': ['x'],
                            'title': 'MyDefaultDc',
                            'type': 'object',
                            'additionalProperties': False,
                        }
                    },
                    'additionalProperties': False,
                    'properties': {
                        'x': {'items': {'type': 'integer'}, 'type': 'array'},
                        'y': {'items': {'$ref': '#/$defs/MyDefaultDc'}, 'type': 'array'},
                    },
                    'required': ['x', 'y'],
                    'type': 'object',
                }
            ),
            snapshot(True),
        ),
        (
            tool_with_tuples,
            None,
            snapshot(
                {
                    'additionalProperties': False,
                    'properties': {
                        'x': {'maxItems': 1, 'minItems': 1, 'prefixItems': [{'type': 'integer'}], 'type': 'array'},
                        'y': {
                            'maxItems': 1,
                            'minItems': 1,
                            'prefixItems': [{'type': 'string'}],
                            'type': 'array',
                        },
                    },
                    'required': ['x'],
                    'type': 'object',
                }
            ),
            snapshot(None),
        ),
        (
            tool_with_tuples,
            True,
            snapshot(
                {
                    'additionalProperties': False,
                    'properties': {
                        'x': {'maxItems': 1, 'minItems': 1, 'prefixItems': [{'type': 'integer'}], 'type': 'array'},
                        'y': {
                            'maxItems': 1,
                            'minItems': 1,
                            'prefixItems': [{'type': 'string'}],
                            'type': 'array',
                        },
                    },
                    'required': ['x', 'y'],
                    'type': 'object',
                }
            ),
            snapshot(True),
        ),
        # (tool, None, snapshot({}), snapshot({})),
        # (tool, True, snapshot({}), snapshot({})),
    ],
)
async def test_strict_mode_cannot_infer_strict(
    allow_model_requests: None,
    tool: Callable[..., Any],
    tool_strict: bool | None,
    expected_params: dict[str, Any],
    expected_strict: bool | None,
):
    """Test that strict mode settings are properly passed to OpenAI and respect precedence rules."""
    # Create a mock completion for testing
    c = completion_message(ChatCompletionMessage(content='world', role='assistant'))

    # Test 1: Default behavior (strict setting not explicitly specified; function is strict-mode-compatible)
    mock_client = MockOpenAI.create_mock(c)
    m = OpenAIModel('gpt-4o', provider=OpenAIProvider(openai_client=mock_client))
    agent = Agent(m)

    agent.tool_plain(strict=tool_strict)(tool)

    await agent.run('hello')
    kwargs = get_mock_chat_completion_kwargs(mock_client)[0]
    assert 'tools' in kwargs, kwargs

    assert kwargs['tools'][0]['function']['parameters'] == expected_params
    actual_strict = kwargs['tools'][0]['function'].get('strict')
    assert actual_strict == expected_strict
    if actual_strict is None:
        # If strict is included, it should be non-None
        assert 'strict' not in kwargs['tools'][0]['function']


def test_strict_schema():
    class Apple(BaseModel):
        kind: Literal['apple'] = 'apple'

    class Banana(BaseModel):
        kind: Literal['banana'] = 'banana'

    class MyModel(BaseModel):
        # We have all these different crazy fields to achieve coverage
        my_recursive: MyModel | None = None
        my_patterns: dict[Annotated[str, Field(pattern='^my-pattern$')], str]
        my_tuple: tuple[int]
        my_list: list[float]
        my_discriminated_union: Annotated[Apple | Banana, Discriminator('kind')]

    assert _StrictSchemaHelper().make_schema_strict(MyModel.model_json_schema()) == snapshot(
        {
            '$defs': {
                'Apple': {
                    'additionalProperties': False,
                    'properties': {'kind': {'const': 'apple', 'default': 'apple', 'title': 'Kind', 'type': 'string'}},
                    'required': ['kind'],
                    'title': 'Apple',
                    'type': 'object',
                },
                'Banana': {
                    'additionalProperties': False,
                    'properties': {'kind': {'const': 'banana', 'default': 'banana', 'title': 'Kind', 'type': 'string'}},
                    'required': ['kind'],
                    'title': 'Banana',
                    'type': 'object',
                },
                'MyModel': {
                    'additionalProperties': False,
                    'properties': {
                        'my_discriminated_union': {
                            'discriminator': {
                                'mapping': {'apple': '#/$defs/Apple', 'banana': '#/$defs/Banana'},
                                'propertyName': 'kind',
                            },
                            'oneOf': [{'$ref': '#/$defs/Apple'}, {'$ref': '#/$defs/Banana'}],
                            'title': 'My Discriminated Union',
                        },
                        'my_list': {'items': {'type': 'number'}, 'title': 'My List', 'type': 'array'},
                        'my_patterns': {
                            'additionalProperties': False,
                            'patternProperties': {'^my-pattern$': {'type': 'string'}},
                            'title': 'My Patterns',
                            'type': 'object',
                        },
                        'my_recursive': {'anyOf': [{'$ref': '#/$defs/MyModel'}, {'type': 'null'}], 'default': None},
                        'my_tuple': {
                            'maxItems': 1,
                            'minItems': 1,
                            'prefixItems': [{'type': 'integer'}],
                            'title': 'My Tuple',
                            'type': 'array',
                        },
                    },
                    'required': ['my_recursive', 'my_patterns', 'my_tuple', 'my_list', 'my_discriminated_union'],
                    'title': 'MyModel',
                    'type': 'object',
                },
            },
            '$ref': '#/$defs/MyModel',
        }
    )


@pytest.mark.vcr
async def test_openai_model_without_system_prompt(allow_model_requests: None, openai_api_key: str):
    m = OpenAIModel('o3-mini', provider=OpenAIProvider(api_key=openai_api_key))
    agent = Agent(m, system_prompt='You are a potato.')
    result = await agent.run()
    assert result.output == snapshot(
        "That's right—I am a potato! A spud of many talents, here to help you out. How can this humble potato be of service today?"
    )


class TestBinaryContentCSV:
    """Test that CSV files are correctly handled in binary content."""

    async def test_openai_model_csv_support(self, csv_content: BinaryContent):
        """Test that OpenAIModel correctly handles CSV files in BinaryContent."""
        # Create a model instance and test its _map_user_prompt directly, which doesn't require a model request
        model = OpenAIModel('gpt-4o', provider=OpenAIProvider(api_key='test-key'))

        # Create a request with CSV binary content
        user_prompt = UserPromptPart(["Here's some CSV data:", csv_content])

        # Test the _map_user_prompt method directly
        mapped_result = await model._map_user_prompt(user_prompt)  # pyright: ignore[reportPrivateUsage]

        # Verify the mapped result contains the CSV data as text
        assert isinstance(mapped_result['content'], list)
        content_parts = mapped_result['content']

        # There should be 2 parts: the text and the decoded CSV
        assert len(content_parts) == 2

        # First part should be the text prompt
        assert content_parts[0]['type'] == 'text'
        assert content_parts[0]['text'] == "Here's some CSV data:"

        # The second part should be the decoded CSV as text
        assert content_parts[1]['type'] == 'text'
        assert 'John,30,New York' in content_parts[1]['text']
        assert 'Alice,25,San Francisco' in content_parts[1]['text']
        assert 'Bob,35,Chicago' in content_parts[1]['text']

    async def test_openai_responses_model_csv_support(self, csv_content: BinaryContent):
        """Test that OpenAIResponsesModel correctly handles CSV files in BinaryContent."""
        # Create a model instance
        model = OpenAIResponsesModel('gpt-4o', provider=OpenAIProvider(api_key='test-key'))

        # Create a request with CSV binary content
        user_prompt = UserPromptPart(["Here's some CSV data:", csv_content])

        # Test the _map_user_prompt method directly
        mapped_result = await model._map_user_prompt(user_prompt)  # pyright: ignore[reportPrivateUsage]

        # Verify the mapped result contains the CSV data as text
        assert isinstance(mapped_result['content'], list)
        content_parts = mapped_result['content']

        # There should be 2 parts: the text and the decoded CSV
        assert len(content_parts) == 2

        # First part should be the text prompt
        assert content_parts[0]['type'] == 'input_text'
        assert content_parts[0]['text'] == "Here's some CSV data:"

        # The second part should be the CSV as a file
        assert content_parts[1]['type'] == 'input_file'
        # For CSV files in OpenAIResponsesModel, the content is in file_data, not text
        assert 'file_data' in content_parts[1]
        # The file_data should contain the CSV content as base64 encoded data
        file_data = content_parts[1]['file_data']
        assert isinstance(file_data, str)
        # We expect it to be base64 encoded, so it should be non-empty
        assert len(file_data) > 0

    async def test_unsupported_binary_content_type(self):
        """Test that unsupported binary content types still raise errors."""
        # Create unsupported binary content
        unsupported_content = BinaryContent(data=b'some binary data', media_type='application/octet-stream')

        # Create a model instance
        model = OpenAIModel('gpt-4o', provider=OpenAIProvider(api_key='test-key'))

        # Create a request with unsupported binary content
        user_prompt = UserPromptPart(["Here's some binary data:", unsupported_content])

        # Verify that it raises the expected error
        with pytest.raises(RuntimeError, match='Unsupported binary content type: application/octet-stream'):
            # Access protected method for testing purposes, ignoring pyright's warning
            await model._map_user_prompt(user_prompt)  # pyright: ignore[reportPrivateUsage]

    async def test_map_user_prompt_with_csv(self):
        """Test that _map_user_prompt correctly handles CSV binary content."""
        model = OpenAIModel('gpt-4o', provider=OpenAIProvider(api_key='test-key'))

        # Create a mock CSV binary content
        csv_content = BinaryContent(data=b'col1,col2\nval1,val2', media_type='text/csv')
        user_prompt = UserPromptPart(["Here's some CSV data:", csv_content])

        # Test the _map_user_prompt method directly
        mapped_result = await model._map_user_prompt(user_prompt)  # pyright: ignore[reportPrivateUsage]

        # Verify the mapped result contains the CSV data as text
        assert isinstance(mapped_result['content'], list)
        content_parts = mapped_result['content']

        # There should be 2 parts: the text and the decoded CSV
        assert len(content_parts) == 2

        # First part should be the text prompt
        assert content_parts[0]['type'] == 'text'
        assert content_parts[0]['text'] == "Here's some CSV data:"

        # The second part should be the decoded CSV as text
        assert content_parts[1]['type'] == 'text'
        assert content_parts[1]['text'] == 'col1,col2\nval1,val2'

    async def test_openai_responses_model_csv_not_document(self):
        """Test that OpenAIResponsesModel correctly handles CSV binary content when is_document is False."""
        # Create a model instance
        model = OpenAIResponsesModel('gpt-4o', provider=OpenAIProvider(api_key='test-key'))

        # Create a subclass of BinaryContent with is_document always returning False
        class MockBinaryContent(BinaryContent):
            @property
            def is_document(self) -> bool:
                return False

        # Create a mock CSV binary content with our subclass
        csv_content = MockBinaryContent(data=b'col1,col2\nval1,val2', media_type='text/csv')

        # Create a request with CSV binary content
        user_prompt = UserPromptPart(["Here's some CSV data:", csv_content])

        # Test the _map_user_prompt method directly
        mapped_result = await model._map_user_prompt(user_prompt)  # pyright: ignore[reportPrivateUsage]

        # Verify the mapped result contains the CSV data as text
        assert isinstance(mapped_result['content'], list)
        content_parts = mapped_result['content']

        # There should be 2 parts: the text and the decoded CSV
        assert len(content_parts) == 2

        # First part should be the text prompt
        assert content_parts[0]['type'] == 'input_text'
        assert content_parts[0]['text'] == "Here's some CSV data:"

        # The second part should be the CSV as text content, not as a file
        assert content_parts[1]['type'] == 'input_text'
        assert content_parts[1]['text'] == 'col1,col2\nval1,val2'
