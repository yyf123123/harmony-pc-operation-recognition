cmake_minimum_required(VERSION 3.16)
project(existing_host_integration LANGUAGES C CXX)
set(BUILD_TESTING OFF CACHE BOOL "" FORCE)
set(HARMONY_OPS_PLATFORM_BRIDGE "${CMAKE_CURRENT_SOURCE_DIR}/platform_bridge.cpp" CACHE FILEPATH "" FORCE)
add_subdirectory(../source ops-core)
# Add platform_bridge.cpp to the existing service target and link this target.
add_library(ops_host_bridge OBJECT platform_bridge.cpp)
target_link_libraries(ops_host_bridge PRIVATE harmony_operation_recognition)
# target_link_libraries(your_existing_service PRIVATE harmony_operation_recognition ops_host_bridge)
