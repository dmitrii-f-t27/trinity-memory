// Standard numeric-conversion primitives, not JSON grammar or schema logic.
// t27 owns syntax validation, Python-compatible presentation and finite checks.
#include <charconv>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <locale.h>
#if defined(__APPLE__)
#include <xlocale.h>
#endif
#include <system_error>

extern "C" size_t tm_float_shortest(double value, uint8_t *output, size_t capacity) {
    if (!output || !capacity || !std::isfinite(value)) return 0;
    auto first = reinterpret_cast<char *>(output);
    // Scientific form requests the shortest significant digits. The no-format
    // overload can prefer a longer exact integer, which loses this contract.
    auto result = std::to_chars(first, first + capacity, value, std::chars_format::scientific);
    return result.ec == std::errc{} ? static_cast<size_t>(result.ptr - first) : 0;
}

extern "C" double tm_json_strtod(uint8_t *text) {
    // strtod_l is independent of an embedding application's LC_NUMERIC.
    // C++ function-local static initialization makes this cache thread-safe.
    struct NumericLocale {
        locale_t value = newlocale(LC_NUMERIC_MASK, "C", nullptr);
        ~NumericLocale() { if (value) freelocale(value); }
    };
    static NumericLocale numeric;
    if (!text || !numeric.value) return NAN;
    char *end = nullptr;
    double value = strtod_l(reinterpret_cast<char *>(text), &end, numeric.value);
    if (end == reinterpret_cast<char *>(text) || *end != '\0') return NAN;
    return value;
}

extern "C" uint64_t tm_float_bits(double value) {
    static_assert(sizeof(value) == sizeof(uint64_t), "IEEE binary64 storage required");
    uint64_t bits;
    std::memcpy(&bits, &value, sizeof(bits));
    return bits;
}
