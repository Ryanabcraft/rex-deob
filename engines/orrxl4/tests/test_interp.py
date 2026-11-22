import unittest

from orrxl4 import sandbox


def prints(src):
    r = sandbox.run(src.encode("latin-1"), timeout=20)
    return [p.decode("latin-1") for p in r.prints], r.error


class Semantics(unittest.TestCase):
    def check(self, src, expected):
        out, error = prints(src)
        self.assertIsNone(error)
        self.assertEqual(out, expected)

    def test_closures_and_shadowing(self):
        self.check(
            "local fs={} for i=1,3 do fs[#fs+1]=function() return i end end\n"
            "local x=1 local function g() return x end local x=2\n"
            "print(g(), x, fs[1](), fs[3]())",
            ["1\t2\t1\t3"],
        )

    def test_varargs_select_pcall(self):
        self.check(
            "local function f(...) return select('#',...), (select(2,...)) end\n"
            "print(f(5,6,7))\n"
            "print(pcall(function() error('boom',0) end))\n"
            "print(pcall(function() local t=nil return t.x end))\n"
            "print(select(-1, 1, 2, 3))",
            ["3\t6", "false\tboom", "false\tattempt to index a nil value", "3"],
        )

    def test_metatables(self):
        self.check(
            "local t=setmetatable({},{__index=function(_,k) return k..'!' end,__call=function(self,a) return a*2 end})\n"
            "print(t.hi, t(21))\n"
            "local c=setmetatable({},{__concat=function(a,b) return 'cat' end})\n"
            "print(c..'x')",
            ["hi!\t42", "cat"],
        )

    def test_strings(self):
        self.check(
            "print(('abc'):rep(2,'-'), ('hello'):sub(-3), #'xyz', ('%05.1f|%x|%s'):format(3.14159, 255, 'q'))\n"
            "print(('a1b22c333'):gsub('%d+','#'))\n"
            "print(('key=val'):match('(%w+)=(%w+)'))\n"
            "for w in ('one two three'):gmatch('%a+') do io=w end print(io)\n"
            "print(('hello world'):find('o w'), ('hello'):find('l+'))\n"
            "print(string.byte('AB',1,2), string.char(72,105))",
            ["abc-abc\tllo\t3\t003.1|ff|q", "a#b#c#\t3", "key\tval", "three", "5\t3\t4", "65\tHi"],
        )

    def test_numbers(self):
        self.check(
            "print(7%3, -7%3, 7/2, 2^10, math.floor(-3.5), math.max(1,9,4), 10//3)\n"
            "print(tostring(1e15), tostring(0.1), 255==255.0, tonumber('0x10'), tonumber('  12  '), tonumber('z',36))",
            ["1\t2\t3.5\t1024\t-4\t9\t3", "1e+15\t0.1\ttrue\t16\t12\t35"],
        )

    def test_control_flow(self):
        self.check(
            "local n=0 repeat local k=n n=n+1 until k>=2 print(n)\n"
            "local i=0 while true do i=i+1 if i>4 then break end end print(i)\n"
            "local function fib(n) if n<2 then return n end return fib(n-1)+fib(n-2) end print(fib(15))\n"
            "local t={} for k,v in pairs({10,20,30,x=1}) do t[#t+1]=tostring(k) end print(table.concat(t,','))",
            ["3", "5", "610", "1,2,3,x"],
        )

    def test_tables(self):
        self.check(
            "local t={5,2,9,1} table.sort(t) print(table.concat(t,' '))\n"
            "table.sort(t,function(a,b) return a>b end) print(table.concat(t,' '))\n"
            "table.insert(t,1,100) table.insert(t,7) print(#t, table.remove(t), table.remove(t,1), #t)\n"
            "print(table.unpack({1,2,3}))",
            ["1 2 5 9", "9 5 2 1", "6\t7\t100\t4", "1\t2\t3"],
        )

    def test_bit32_and_fallback_xor(self):
        self.check(
            "print(bit32.bxor(0xF0,0x3C), bit32.band(12,10), bit32.lshift(1,4))\n"
            "local function x(a,b) local c,r=1,0 while a>0 or b>0 do if a%2~=b%2 then r=r+c end a,b,c=math.floor(a/2),math.floor(b/2),c*2 end return r end\n"
            "print(x(0xF0,0x3C))",
            ["204\t8\t16", "204"],
        )

    def test_loadstring_is_captured_not_executed(self):
        r = sandbox.run(b'local f=loadstring("print(1)") f() print(2)', timeout=10)
        self.assertEqual(r.captures, [b"print(1)"])
        self.assertEqual(r.prints, [b"2"])

    def test_sandbox_has_no_host_access(self):
        out, error = prints("print(type(io), type(os.execute), type(debug), type(require), type(game))")
        self.assertIsNone(error)
        self.assertEqual(out, ["nil\tnil\tnil\tnil\tnil"])

    def test_timeout(self):
        r = sandbox.run(b"while true do end", timeout=0.5)
        self.assertIn("time limit", r.error)


if __name__ == "__main__":
    unittest.main()
