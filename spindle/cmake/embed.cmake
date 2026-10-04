# Turns a text file into a C++ byte array (used for the HLSL source), so the
# executable is self-contained and no string-literal length limits apply.
#   cmake -DINPUT=... -DOUTPUT=... -DSYMBOL=... -P embed.cmake
file(READ "${INPUT}" content HEX)
string(LENGTH "${content}" hexlen)
math(EXPR size "${hexlen} / 2")
string(REGEX REPLACE "([0-9a-f][0-9a-f])" "0x\\1," bytes "${content}")
file(WRITE "${OUTPUT}"
"// Generated from ${INPUT}. Do not edit.\n"
"namespace spindle {\n"
"extern const unsigned char ${SYMBOL}[] = {${bytes}0};\n"
"extern const unsigned long long ${SYMBOL}Size = ${size}ULL;\n"
"}\n")
