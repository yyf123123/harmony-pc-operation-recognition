cmake_minimum_required(VERSION 3.16)
project(existing_host_integration LANGUAGES C CXX)
set(BUILD_TESTING OFF)
set(HARMONY_OPS_PLATFORM_BRIDGE "${CMAKE_CURRENT_SOURCE_DIR}/platform_bridge.cpp" CACHE FILEPATH "" FORCE)
add_subdirectory(../source ops-core)
# The host target already owns the single editable bridge and the worker.
# Existing host: add_subdirectory(/package/operator ops)
#               target_link_libraries(your_service PRIVATE harmony_operation_host)
