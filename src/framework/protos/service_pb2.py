# -*- coding: utf-8 -*-
# Generated by the protocol buffer compiler.  DO NOT EDIT!
# source: framework/protos/service.proto
"""Generated protocol buffer code."""
from google.protobuf.internal import builder as _builder
from google.protobuf import descriptor as _descriptor
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import symbol_database as _symbol_database

# @@protoc_insertion_point(imports)

_sym_db = _symbol_database.Default()

from framework.protos import message_pb2 as framework_dot_protos_dot_message__pb2

DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(
    b'\n\x1e\x66ramework/protos/service.proto\x12\x10\x66ramework.protos\x1a\x1e\x66ramework/protos/message.proto2\x9f\x02\n\x0eMessageService\x12<\n\x04send\x12\x19.framework.protos.Message\x1a\x19.framework.protos.Message\x12\x42\n\x08register\x12\x19.framework.protos.Message\x1a\x19.framework.protos.Message0\x01\x12\x42\n\nunregister\x12\x19.framework.protos.Message\x1a\x19.framework.protos.Message\x12G\n\x0bsend_stream\x12\x19.framework.protos.Message\x1a\x19.framework.protos.Message(\x01\x30\x01\x62\x06proto3')

_builder.BuildMessageAndEnumDescriptors(DESCRIPTOR, globals())
_builder.BuildTopDescriptorsAndMessages(DESCRIPTOR, 'framework.protos.service_pb2', globals())
if _descriptor._USE_C_DESCRIPTORS == False:
    DESCRIPTOR._options = None
    _MESSAGESERVICE._serialized_start = 85
    _MESSAGESERVICE._serialized_end = 372
# @@protoc_insertion_point(module_scope)
