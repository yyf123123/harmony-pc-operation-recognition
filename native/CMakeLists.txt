add_library(harmony_operation_recognition STATIC
    src/recognition.cpp
    src/feature.cpp
    src/pipeline.cpp
    src/runtime.cpp
    src/replay.cpp
    src/service_c.cpp)
target_include_directories(harmony_operation_recognition PUBLIC include)
target_compile_features(harmony_operation_recognition PUBLIC cxx_std_17)
target_link_libraries(harmony_operation_recognition PUBLIC harmony_vma_collector Threads::Threads)
if(MSVC)
    target_compile_options(harmony_operation_recognition PRIVATE /W4)
else()
    target_compile_options(harmony_operation_recognition PRIVATE -Wall -Wextra -Wpedantic)
endif()

add_executable(operation_predict examples/offline_predict.cpp)
target_link_libraries(operation_predict PRIVATE harmony_operation_recognition)

add_executable(operation_feature examples/feature_vector.cpp)
target_link_libraries(operation_feature PRIVATE harmony_operation_recognition)

set(HARMONY_OPS_PLATFORM_BRIDGE "${CMAKE_CURRENT_SOURCE_DIR}/examples/platform_bridge.cpp" CACHE FILEPATH "Host focus/output bridge")
add_executable(opsctl examples/opsctl.cpp examples/opsctl_platform.cpp "${HARMONY_OPS_PLATFORM_BRIDGE}")
target_link_libraries(opsctl PRIVATE harmony_operation_recognition)

# Compile the service-side interface skeleton without registering a service.
add_library(operation_adapter_template OBJECT examples/service_adapter_template.cpp "${HARMONY_OPS_PLATFORM_BRIDGE}")
target_link_libraries(operation_adapter_template PRIVATE harmony_operation_recognition)

if(BUILD_TESTING)
    add_executable(operation_pipeline_test tests/pipeline_test.cpp)
    target_link_libraries(operation_pipeline_test PRIVATE harmony_operation_recognition)
    add_test(NAME operation_pipeline_test
             COMMAND operation_pipeline_test ${PROJECT_SOURCE_DIR}/models)
    add_executable(runtime_test tests/runtime_test.cpp)
    target_link_libraries(runtime_test PRIVATE harmony_operation_recognition)
    add_test(NAME runtime_test COMMAND runtime_test ${PROJECT_SOURCE_DIR}/models ${PROJECT_SOURCE_DIR}/native/fixtures/raw-window)
    add_executable(service_c_test tests/service_c_test.c)
    set_target_properties(service_c_test PROPERTIES C_STANDARD 11 LINKER_LANGUAGE CXX)
    target_link_libraries(service_c_test PRIVATE harmony_operation_recognition)
    add_test(NAME service_c_test COMMAND service_c_test ${PROJECT_SOURCE_DIR}/models ${PROJECT_SOURCE_DIR}/native/fixtures/raw-window)
endif()

install(TARGETS harmony_operation_recognition harmony_vma_collector opsctl
    ARCHIVE DESTINATION lib RUNTIME DESTINATION bin)
install(DIRECTORY include/ ${PROJECT_SOURCE_DIR}/collector/include/ DESTINATION include)
