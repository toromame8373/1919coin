# rxhelper build: links the RandomX static library bundled with the node.
# Prerequisite: the 1919coin tree has been built (../build/src/randomx/librandomx.a).

RANDOMX_A := ../build/src/randomx/librandomx.a
RANDOMX_H := ../1919coin/src/randomx/src

rxhelper: rxhelper.cpp $(RANDOMX_A)
	g++ -O2 -std=c++17 -I$(RANDOMX_H) -o $@ rxhelper.cpp $(RANDOMX_A) -lpthread

clean:
	rm -f rxhelper

.PHONY: clean
