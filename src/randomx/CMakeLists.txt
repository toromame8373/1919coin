# Minimal CMake build of RandomX (tevador/RandomX v1.2.3, BSD-3-Clause) embedded in this tree.
enable_language(C)
# Only the static library is built (no tests/benchmarks). configuration.asm is pre-generated.

set(RANDOMX_SRC ${CMAKE_CURRENT_SOURCE_DIR}/src)

set(randomx_sources
  ${RANDOMX_SRC}/aes_hash.cpp
  ${RANDOMX_SRC}/argon2_ref.c
  ${RANDOMX_SRC}/argon2_ssse3.c
  ${RANDOMX_SRC}/argon2_avx2.c
  ${RANDOMX_SRC}/bytecode_machine.cpp
  ${RANDOMX_SRC}/cpu.cpp
  ${RANDOMX_SRC}/dataset.cpp
  ${RANDOMX_SRC}/soft_aes.cpp
  ${RANDOMX_SRC}/virtual_memory.c
  ${RANDOMX_SRC}/vm_interpreted.cpp
  ${RANDOMX_SRC}/allocator.cpp
  ${RANDOMX_SRC}/assembly_generator_x86.cpp
  ${RANDOMX_SRC}/instruction.cpp
  ${RANDOMX_SRC}/randomx.cpp
  ${RANDOMX_SRC}/superscalar.cpp
  ${RANDOMX_SRC}/vm_compiled.cpp
  ${RANDOMX_SRC}/vm_interpreted_light.cpp
  ${RANDOMX_SRC}/argon2_core.c
  ${RANDOMX_SRC}/blake2_generator.cpp
  ${RANDOMX_SRC}/instructions_portable.cpp
  ${RANDOMX_SRC}/reciprocal.c
  ${RANDOMX_SRC}/virtual_machine.cpp
  ${RANDOMX_SRC}/vm_compiled_light.cpp
  ${RANDOMX_SRC}/blake2/blake2b.c
)

string(TOLOWER "${CMAKE_SYSTEM_PROCESSOR}" RANDOMX_ARCH_ID)
if(MSVC AND CMAKE_GENERATOR_PLATFORM)
  string(TOLOWER "${CMAKE_GENERATOR_PLATFORM}" RANDOMX_ARCH_ID)
endif()

set(RANDOMX_X86_64 FALSE)
set(RANDOMX_ARM64 FALSE)
if(CMAKE_SIZEOF_VOID_P EQUAL 8)
  if(RANDOMX_ARCH_ID MATCHES "^(x86_64|x86-64|amd64|x64)$")
    set(RANDOMX_X86_64 TRUE)
  elseif(RANDOMX_ARCH_ID MATCHES "^(aarch64|arm64|armv8-a)$")
    set(RANDOMX_ARM64 TRUE)
  endif()
endif()

if(RANDOMX_X86_64)
  list(APPEND randomx_sources ${RANDOMX_SRC}/jit_compiler_x86.cpp)
  if(MSVC)
    enable_language(ASM_MASM)
    list(APPEND randomx_sources ${RANDOMX_SRC}/jit_compiler_x86_static.asm)
    set_property(SOURCE ${RANDOMX_SRC}/jit_compiler_x86_static.asm PROPERTY LANGUAGE ASM_MASM)
    set_source_files_properties(${RANDOMX_SRC}/argon2_avx2.c PROPERTIES COMPILE_FLAGS /arch:AVX2)
  else()
    enable_language(ASM)
    list(APPEND randomx_sources ${RANDOMX_SRC}/jit_compiler_x86_static.S)
    set_source_files_properties(${RANDOMX_SRC}/argon2_ssse3.c PROPERTIES COMPILE_FLAGS -mssse3)
    set_source_files_properties(${RANDOMX_SRC}/argon2_avx2.c PROPERTIES COMPILE_FLAGS -mavx2)
  endif()
elseif(RANDOMX_ARM64)
  enable_language(ASM)
  list(APPEND randomx_sources
    ${RANDOMX_SRC}/jit_compiler_a64_static.S
    ${RANDOMX_SRC}/jit_compiler_a64.cpp)
endif()

add_library(randomx STATIC ${randomx_sources})
target_include_directories(randomx PUBLIC ${RANDOMX_SRC})
set_target_properties(randomx PROPERTIES
  POSITION_INDEPENDENT_CODE ON
  C_STANDARD 11
  CXX_STANDARD 17
  CXX_STANDARD_REQUIRED ON
)
if(NOT MSVC)
  if(RANDOMX_X86_64)
    target_compile_options(randomx PRIVATE -maes)
  elseif(RANDOMX_ARM64)
    target_compile_options(randomx PRIVATE -march=armv8-a+crypto)
  endif()
  target_compile_options(randomx PRIVATE -w)
else()
  target_compile_options(randomx PRIVATE /w)
endif()
